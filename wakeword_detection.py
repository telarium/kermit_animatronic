#!/usr/bin/env python3
import os
import queue
import threading
import time
import numpy as np
from pydispatch import dispatcher
import mic_stream
from openwakeword.model import Model


class WakeWord:
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
			# a new one, so two threads can't both be feeding the model.
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
			# Wait for the thread to actually finish before returning.
			if self._thread is not None and self._thread.is_alive():
				self._thread.join(timeout=4.0)
			print("WakeWord: listening stopped.")

	def wait_until_stopped(self, timeout: float = 4.0) -> bool:
		"""Block until the mic stream has fully closed. Returns True if stopped in time."""
		return self._stopped_event.wait(timeout=timeout)

	# -------------------------------------------------------------------------
	# Internal
	# -------------------------------------------------------------------------

	def _listen_loop(self) -> None:
		"""Consume the shared mic stream and run the wakeword model on it.

		This used to own an arecord process, with a settle sleep, a
		no-audio retry counter, and a terminate/wait teardown — all of which
		existed to manage handing the ALSA device to STT. The device is no
		longer handed over, so all of that is gone.
		"""
		q = None
		try:
			q = mic_stream.subscribe()
			while not self._stop_event.is_set():
				try:
					# Timeout rather than block forever, so _stop_event is
					# still honoured if capture stalls.
					audio_mono = q.get(timeout=0.5)
				except queue.Empty:
					continue

				prediction = self._oww.predict(audio_mono)
				score = prediction.get(os.path.splitext(os.path.basename(self.model_path))[0], 0)

				if score > self.threshold:
					print(f"Wakeword detected! (score: {score:.2f})")
					# Timestamp detection so STT can pull the audio that
					# follows it out of the ring buffer, including whatever
					# was said while the acknowledgement animation played.
					mic_stream.set_anchor()
					self._stop_event.set()
					self._enabled = False
					self._oww.reset()
					dispatcher.send(signal="wakewordEvent")
					if self.on_detected:
						self.on_detected(score)

		except Exception as e:
			print(f"WakeWord: error in listen loop: {e}")
		finally:
			if q is not None:
				mic_stream.unsubscribe(q)
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
