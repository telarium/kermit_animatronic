#!/usr/bin/env python3
import os
import subprocess
import threading
import time
import numpy as np
import pyaudio
from pydispatch import dispatcher
from openwakeword.model import Model


class WakeWord:
	MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib/openwakeword/hey_ker_mit.onnx")
	XVF_PY     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib/respeaker/python_control/xvf_host.py")

	CHUNK    = 1280
	FORMAT   = pyaudio.paInt16
	CHANNELS = 2
	RATE     = 16000

	def __init__(self, on_detected=None):
		self.on_detected = on_detected
		self._enabled = False
		self._thread = None
		self._stop_event = threading.Event()
		self.threshold: float = 0.3

		_devnull = open(os.devnull, 'w')
		_old_stderr = os.dup(2)
		os.dup2(_devnull.fileno(), 2)
		self._oww = Model(
			wakeword_models=[self.MODEL_PATH],
			inference_framework="onnx",
			vad_threshold=0.0
		)
		self._pa = pyaudio.PyAudio()
		os.dup2(_old_stderr, 2)
		os.close(_old_stderr)
		_devnull.close()

		print(f"WakeWord: model loaded from {self.MODEL_PATH}")

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
			self._enabled = True
			self._stop_event.clear()
			self._thread = threading.Thread(target=self._listen_loop, daemon=True)
			self._thread.start()
			dispatcher.send(signal="updateStatus", id="Voice Command Status", value="Waiting for 'Hey Kermit'...")
			print("WakeWord: listening started.")
		elif not enabled and self._enabled:
			self._enabled = False
			self._stop_event.set()
			if self._thread:
				self._thread.join(timeout=3)
				self._thread = None
			print("WakeWord: listening stopped.")

	# -------------------------------------------------------------------------
	# Internal
	# -------------------------------------------------------------------------

	def _find_device_index(self) -> int:
		for attempt in range(20):
			self._pa.terminate()
			_devnull = open(os.devnull, 'w')
			_old_stderr = os.dup(2)
			os.dup2(_devnull.fileno(), 2)
			self._pa = pyaudio.PyAudio()
			os.dup2(_old_stderr, 2)
			os.close(_old_stderr)
			_devnull.close()

			for i in range(self._pa.get_device_count()):
				d = self._pa.get_device_info_by_index(i)
				if d['maxInputChannels'] > 0 and 'respeaker' in d['name'].lower():
					print(f"WakeWord: found ReSpeaker at index {i} — {d['name']}")
					return i
			print(f"WakeWord: ReSpeaker not found, retrying ({attempt + 1}/20)...")
			time.sleep(1)
		raise RuntimeError("ReSpeaker not found — is it plugged in?")

	def _open_stream(self, device_index: int) -> pyaudio.Stream:
		_devnull = open(os.devnull, 'w')
		_old_stderr = os.dup(2)
		os.dup2(_devnull.fileno(), 2)
		stream = self._pa.open(
			format=self.FORMAT,
			channels=self.CHANNELS,
			rate=self.RATE,
			input=True,
			frames_per_buffer=self.CHUNK,
			input_device_index=device_index,
		)
		os.dup2(_old_stderr, 2)
		os.close(_old_stderr)
		_devnull.close()
		return stream

	def _listen_loop(self) -> None:
		while not self._stop_event.is_set():
			try:
				device_index = self._find_device_index()
				stream = self._open_stream(device_index)

				try:
					while not self._stop_event.is_set():
						audio = stream.read(self.CHUNK, exception_on_overflow=False)
						audio_np = np.frombuffer(audio, dtype=np.int16).reshape(-1, 2)
						audio_mono = audio_np[:, 0]

						prediction = self._oww.predict(audio_mono)
						score = prediction.get("hey_ker_mit", 0)

						#if score > 0.05:
						#	print(f"score: {score:.3f}")

						if score > self.threshold:
							print(f"'Hey Kermit' detected! (score: {score:.2f})")
							dispatcher.send(signal="wakewordEvent")
							self._oww.reset()
							if self.on_detected:
								self.on_detected(score)
				finally:
					try:
						stream.stop_stream()
						stream.close()
					except Exception:
						pass

			except Exception as e:
				print(f"WakeWord: error in listen loop: {e}")
				time.sleep(2)

	def __del__(self):
		self.set_enabled(False)
		self._pa.terminate()


if __name__ == "__main__":
	def on_detected(score):
		print(f">> Hey Kermit heard! score={score:.2f}")

	ww = WakeWord(on_detected=on_detected)
	ww.set_enabled(True)

	try:
		while True:
			time.sleep(1)
	except KeyboardInterrupt:
		ww.set_enabled(False)
		print("Exiting.")