#!/usr/bin/env python3
import os
import subprocess
import threading
import tempfile
import wave
import time
import numpy as np
import requests
from pydispatch import dispatcher


class SpeechToText:
	WHISPER_SERVER_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib/whisper/build/bin/whisper-server")
	WHISPER_MODEL      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib/whisper/models/ggml-base.en.bin")
	WHISPER_URL        = "http://127.0.0.1:8080/inference"

	SAMPLE_RATE        = 16000
	CHUNK_SIZE         = 1024
	SILENCE_THRESHOLD  = 300
	SILENCE_CHUNKS     = 15
	MIN_SPEECH_CHUNKS  = 4
	PREROLL_CHUNKS     = 8

	def __init__(self) -> None:
		self._server_proc = None
		self._listen_thread = None
		self._listening = False
		self._alsa_device = self._find_alsa_device()

		self._start_whisper_server()
		print("SpeechToText: initialized.")

	def _find_alsa_device(self) -> str:
		"""Find the USB mic card number and return the plughw device string."""
		result = subprocess.run(["arecord", "-l"], capture_output=True, text=True)
		for line in result.stdout.splitlines():
			if any(k in line.lower() for k in ['usb', 'respeaker']):
				parts = line.split(":")
				card_num = parts[0].replace("card", "").strip()
				print(f"SpeechToText: found mic at card {card_num}")
				return f"plughw:{card_num},0"
		print("SpeechToText: USB mic not found, falling back to plughw:0,0")
		return "plughw:0,0"

	def _start_whisper_server(self) -> None:
		"""Launch whisper-server as a background subprocess."""
		print("SpeechToText: starting whisper-server...")
		self._server_proc = subprocess.Popen(
			[
				self.WHISPER_SERVER_BIN,
				"-m", self.WHISPER_MODEL,
				"--host", "127.0.0.1",
				"--port", "8080",
			],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		)
		# Give the server a moment to initialize
		time.sleep(2)
		print("SpeechToText: whisper-server ready.")

	def listen_once(self) -> None:
		"""Begin capturing audio in a background thread.
		Dispatches 'transcriptionResult' with the text when done."""
		if self._listening:
			print("SpeechToText: already listening, ignoring request.")
			return
		self._listen_thread = threading.Thread(target=self._capture_and_transcribe, daemon=True)
		self._listen_thread.start()

	def _capture_and_transcribe(self) -> None:
		self._listening = True
		print("SpeechToText: listening...")

		arecord_cmd = [
			"arecord",
			"-D", self._alsa_device,
			"-f", "S16_LE",
			"-r", str(self.SAMPLE_RATE),
			"-c", "2",  # stereo — ReSpeaker outputs beamformed audio on left channel
			"--buffer-size=4096",
			"-t", "raw",
		]

		proc = subprocess.Popen(arecord_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

		speech_frames  = []
		silence_count  = 0
		in_speech      = False
		preroll_buffer = []

		try:
			while True:
				raw = proc.stdout.read(self.CHUNK_SIZE * 2 * 2)  # *2 for int16, *2 for stereo
				if not raw:
					break

				# Extract left channel (beamformed output)
				samples = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2)
				data = samples[:, 0].tobytes()

				energy = self._rms(data)

				if energy > self.SILENCE_THRESHOLD:
					if not in_speech:
						in_speech = True
						silence_count = 0
						speech_frames = list(preroll_buffer)
					speech_frames.append(data)
					preroll_buffer = []
				else:
					preroll_buffer.append(data)
					if len(preroll_buffer) > self.PREROLL_CHUNKS:
						preroll_buffer.pop(0)

					if in_speech:
						silence_count += 1
						speech_frames.append(data)
						if silence_count >= self.SILENCE_CHUNKS:
							# Done capturing — transcribe
							if len(speech_frames) >= self.MIN_SPEECH_CHUNKS:
								text = self._transcribe(speech_frames)
								if text:
									print(f"SpeechToText: transcribed: {text}")
									dispatcher.send(signal="transcriptionResult", text=text)
							break

		finally:
			proc.terminate()
			self._listening = False

	def _rms(self, data: bytes) -> float:
		samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
		return np.sqrt(np.mean(samples**2))

	def _transcribe(self, frames: list) -> str:
		audio = np.frombuffer(b''.join(frames), dtype=np.int16)

		with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
			tmp_path = f.name
		wf = wave.open(tmp_path, 'wb')
		wf.setnchannels(1)
		wf.setsampwidth(2)
		wf.setframerate(self.SAMPLE_RATE)
		wf.writeframes(audio.tobytes())
		wf.close()

		try:
			with open(tmp_path, 'rb') as f:
				response = requests.post(
					self.WHISPER_URL,
					files={'file': ('audio.wav', f, 'audio/wav')},
					data={'temperature': '0', 'response_format': 'json'}
				)
			if response.ok:
				return response.json().get('text', '').strip()
		except Exception as e:
			print(f"SpeechToText: transcription error: {e}")
		finally:
			os.unlink(tmp_path)

		return ''

	def shutdown(self) -> None:
		"""Stop listening and kill the whisper-server process."""
		self._listening = False
		if self._server_proc:
			self._server_proc.terminate()
			self._server_proc = None
		print("SpeechToText: shutdown complete.")