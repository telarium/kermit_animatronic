#!/usr/bin/env python3
import os
import threading
import numpy as np
import scipy.signal
import pyaudio
from pydispatch import dispatcher
from openwakeword.model import Model


class WakeWord:
	MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib/openwakeword/hey_ker_mit.onnx")

	CHUNK = 3528        # 44100 / 16000 * 1280 — 80ms at 44100Hz
	FORMAT = pyaudio.paInt16
	CHANNELS = 1
	RATE = 44100
	TARGET_RATE = 16000
	THRESHOLD = 0.5

	def __init__(self, on_detected=None):
		"""
		on_detected: optional callback called when wake word is detected.
					 Receives the score as a float argument.
		"""
		self.on_detected = on_detected
		self._enabled = False
		self._thread = None
		self._stop_event = threading.Event()

		# Suppress ALSA noise during pyaudio and model init
		_devnull = open(os.devnull, 'w')
		_old_stderr = os.dup(2)
		os.dup2(_devnull.fileno(), 2)
		self._oww = Model(wakeword_models=[self.MODEL_PATH], inference_framework="onnx")
		self._pa = pyaudio.PyAudio()
		os.dup2(_old_stderr, 2)
		os.close(_old_stderr)
		_devnull.close()

		print(f"WakeWord: model loaded from {self.MODEL_PATH}")

	def _find_device_index(self) -> int:
		"""Find the USB microphone by name, regardless of reboot-assigned index."""
		for i in range(self._pa.get_device_count()):
			d = self._pa.get_device_info_by_index(i)
			if d['maxInputChannels'] > 0 and 'usb microphone' in d['name'].lower():
				print(f"WakeWord: found mic at index {i} — {d['name']}")
				return i
		raise RuntimeError("USB microphone not found — is it plugged in?")

	def set_enabled(self, enabled: bool) -> None:
		"""Start or stop listening for the wake word."""
		if enabled and not self._enabled:
			self._enabled = True
			self._stop_event.clear()
			self._thread = threading.Thread(target=self._listen_loop, daemon=True)
			self._thread.start()
			print("WakeWord: listening started.")
		elif not enabled and self._enabled:
			self._enabled = False
			self._stop_event.set()
			if self._thread:
				self._thread.join(timeout=3)
				self._thread = None
			print("WakeWord: listening stopped.")

	def _listen_loop(self) -> None:
		device_index = self._find_device_index()

		# Suppress ALSA noise when opening the stream
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

		try:
			while not self._stop_event.is_set():
				audio = stream.read(self.CHUNK, exception_on_overflow=False)
				audio_np = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
				# Resample from 44100Hz to 16000Hz
				resampled = scipy.signal.resample_poly(audio_np, self.TARGET_RATE, self.RATE)
				resampled = resampled.astype(np.int16)
				prediction = self._oww.predict(resampled)
				score = prediction.get("hey_ker_mit", 0)
				if score > self.THRESHOLD:
					print(f"'Hey Kermit' detected! (score: {score:.2f})")
					dispatcher.send(signal="wakewordEvent")
					self._oww.reset()
					if self.on_detected:
						self.on_detected(score)
		finally:
			stream.stop_stream()
			stream.close()

	def __del__(self):
		self.set_enabled(False)
		self._pa.terminate()


if __name__ == "__main__":
	import time

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
