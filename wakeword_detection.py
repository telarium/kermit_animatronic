#!/usr/bin/env python3
import os
import re
import subprocess
import threading
import time
import numpy as np
from pydispatch import dispatcher
from openwakeword.model import Model


class WakeWord:
	XVF_PY  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib/respeaker/python_control/xvf_host.py")

	CHUNK    = 1280
	CHANNELS = 2
	RATE     = 16000

	def __init__(self, model_path: str, description: str, on_detected=None):
		self.model_path = model_path
		self.description = description
		self.on_detected = on_detected
		self._enabled = False
		self._thread = None
		self._stop_event = threading.Event()
		# Callers can wait on this to know the mic stream has fully closed.
		self._stopped_event = threading.Event()
		self._stopped_event.set()  # starts in the "stopped" state
		self.threshold: float = 0.3
		self._last_logged_device = None

		_devnull = open(os.devnull, 'w')
		_old_stderr = os.dup(2)
		os.dup2(_devnull.fileno(), 2)
		self._oww = Model(
			wakeword_models=[self.model_path],
			inference_framework="onnx",
			vad_threshold=0.0
		)
		os.dup2(_old_stderr, 2)
		os.close(_old_stderr)
		_devnull.close()

		print(f"WakeWord: model loaded from {self.model_path}")

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

	def apply_config(self, path: str) -> None:
		import configparser
		config = configparser.ConfigParser()
		try:
			config.read(path)
		except configparser.Error as e:
			print(f"WakeWord: failed to parse config at '{path}': {e}")
			return
		self.threshold = config.getfloat("Wakeword", "Threshold", fallback=0.3)
		print(f"WakeWord: threshold set to {self.threshold}")

	def set_enabled(self, enabled: bool) -> None:
		if enabled and not self._enabled:
			# Ensure any previous listen thread has fully exited before starting
			# a new one — otherwise two threads (each with its own arecord) can
			# briefly contend for the mic, corrupting the capture stream.
			if self._thread is not None and self._thread.is_alive():
				self._stop_event.set()
				self._thread.join(timeout=4.0)
			self._enabled = True
			self._stop_event.clear()
			self._stopped_event.clear()
			self._thread = threading.Thread(target=self._listen_loop, daemon=True)
			self._thread.start()
			dispatcher.send(signal="updateStatus", id="Voice Command Status", value=f"Waiting for '{self.description}'...")
			print("WakeWord: listening started.")
		elif not enabled and self._enabled:
			self._enabled = False
			self._stop_event.set()
			# Wait for the thread to actually finish and release the mic, so a
			# following set_enabled(True) can't create an overlapping capture.
			if self._thread is not None and self._thread.is_alive():
				self._thread.join(timeout=4.0)
			print("WakeWord: listening stopped.")

	def wait_until_stopped(self, timeout: float = 4.0) -> bool:
		"""Block until the mic stream has fully closed. Returns True if stopped in time."""
		return self._stopped_event.wait(timeout=timeout)

	# -------------------------------------------------------------------------
	# Internal
	# -------------------------------------------------------------------------

	def _respeaker_alsa_device(self) -> str:
		"""Find the ReSpeaker's ALSA capture device string (e.g. plughw:0,0).

		Reads `arecord -l` directly — ALSA always lists the device correctly
		(confirmed even when PortAudio's cache goes stale), so this never gets
		into the "device not found" state the old PyAudio path suffered.
		"""
		for attempt in range(20):
			try:
				out = subprocess.run(
					["arecord", "-l"], capture_output=True, text=True
				).stdout
				for line in out.splitlines():
					# e.g. "card 0: Array [reSpeaker XVF3800 4-Mic Array], device 0: ..."
					if "respeaker" in line.lower() and line.strip().lower().startswith("card"):
						# parse "card N" and "device M"
						m = re.search(r"card (\d+):.*device (\d+):", line)
						if m:
							dev = f"plughw:{m.group(1)},{m.group(2)}"
							if dev != self._last_logged_device:
								print(f"WakeWord: found ReSpeaker at {dev}")
								self._last_logged_device = dev
							return dev
			except Exception as e:
				print(f"WakeWord: error scanning arecord -l: {e}")
			print(f"WakeWord: ReSpeaker not found, retrying ({attempt + 1}/20)...")
			time.sleep(1)
		raise RuntimeError("ReSpeaker not found — is it plugged in?")

	def _listen_loop(self) -> None:
		try:
			consecutive_failures = 0
			while not self._stop_event.is_set():
				proc = None
				try:
					device = self._respeaker_alsa_device()
					# Brief settle: if STT or a prior cycle just released the mic,
					# the ALSA capture device can need a moment before it yields
					# samples. A short wait here avoids opening into a half-freed
					# device that produces no audio.
					time.sleep(0.2)
					# Capture raw PCM from the ReSpeaker via arecord — same
					# mechanism STT uses, immune to PortAudio device caching.
					# S16_LE stereo @ 16k matches the old PyAudio config; we take
					# the left channel as mono for the wakeword model.
					bytes_per_frame = 2 * self.CHANNELS          # int16 * channels
					chunk_bytes = self.CHUNK * bytes_per_frame
					proc = subprocess.Popen(
						[
							"arecord",
							"-D", device,
							"-f", "S16_LE",
							"-r", str(self.RATE),
							"-c", str(self.CHANNELS),
							"-t", "raw",
						],
						stdout=subprocess.PIPE,
						stderr=subprocess.DEVNULL,
					)

					got_audio = False
					while not self._stop_event.is_set():
						audio = proc.stdout.read(chunk_bytes)
						if not audio or len(audio) < chunk_bytes:
							# arecord died or short read — break to re-open.
							break
						got_audio = True
						consecutive_failures = 0
						audio_np = np.frombuffer(audio, dtype=np.int16).reshape(-1, self.CHANNELS)
						audio_mono = audio_np[:, 0]

						prediction = self._oww.predict(audio_mono)
						score = prediction.get(os.path.splitext(os.path.basename(self.model_path))[0], 0)

						if score > self.threshold:
							print(f"Wakeword detected! (score: {score:.2f})")
							self._stop_event.set()
							self._enabled = False
							self._oww.reset()
							# Dispatch after stopping so the handler can safely open
							# the mic (arecord) without contending with this capture.
							dispatcher.send(signal="wakewordEvent")
							if self.on_detected:
								self.on_detected(score)
							# Inner loop exits because _stop_event is now set.

					# If arecord produced no audio at all before dying, the mic
					# was likely still held by a prior process (STT or the last
					# cycle). Back off briefly so we don't tight-spin.
					if not got_audio and not self._stop_event.is_set():
						consecutive_failures += 1
						time.sleep(0.3)
						if consecutive_failures >= 30:
							print("WakeWord: mic not yielding audio after many retries.")
							consecutive_failures = 0
				except Exception as e:
					print(f"WakeWord: error in listen loop: {e}")
					time.sleep(2)
				finally:
					# Always stop the arecord process so it releases the mic —
					# this is what lets STT's arecord grab it next.
					if proc is not None:
						try:
							proc.terminate()
							proc.wait(timeout=2)
						except Exception:
							try:
								proc.kill()
							except Exception:
								pass

		finally:
			# Signal that the mic stream is fully closed, whatever the exit reason.
			self._stopped_event.set()
			print("WakeWord: listen loop exited.")

	def __del__(self):
		self.set_enabled(False)


if __name__ == "__main__":
	def on_detected(score):
		print(f">> Wakeword heard! score={score:.2f}")

	ww = WakeWord(
		model_path="lib/openwakeword/okay_ker_mit.onnx",
		description="Okay Kermit",
		on_detected=on_detected,
	)
	ww.set_enabled(True)

	try:
		while True:
			time.sleep(1)
	except KeyboardInterrupt:
		ww.set_enabled(False)
		print("Exiting.")
