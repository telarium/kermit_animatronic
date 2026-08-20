#!/usr/bin/env python3
import os
import subprocess
import threading
import tempfile
import wave
import time
import numpy as np
import requests
import webrtcvad
from collections import deque
from pydispatch import dispatcher


class SpeechToText:
	WHISPER_SERVER_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib/whisper/build/bin/whisper-server")
	WHISPER_MODEL      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib/whisper/models/ggml-base.en.bin")
	WHISPER_URL        = "http://127.0.0.1:8080/inference"

	# Beam search width. 1 = greedy decoding, which commits to the highest
	# probability token at each step and cannot revise it
	# Wider beams score whole candidate sequences instead, at the
	# cost of decode time. 5 is whisper.cpp's own CLI default.
	BEAM_SIZE          = 5

	SAMPLE_RATE        = 16000
	VAD_FRAME_MS       = 30                                       # webrtcvad supports 10, 20, or 30ms
	VAD_FRAME_SAMPLES  = int(SAMPLE_RATE * VAD_FRAME_MS / 1000)  # 480 samples
	VAD_FRAME_BYTES    = VAD_FRAME_SAMPLES * 2                    # 960 bytes (int16)
	VAD_AGGRESSIVENESS = 3                                        # 0–3; 3 = most aggressive noise filtering

	# Adaptive noise floor settings
	NOISE_FLOOR_MIN    = 50                                       # never go below this (avoids hypersensitivity in silence)
	NOISE_FLOOR_WINDOW = 67                                       # frames to average for noise floor (~2 seconds at 30ms/frame)
	SPEECH_RATIO       = 3.0                                      # speech threshold = floor * this ratio

	PREROLL_FRAMES     = 8                                        # frames before speech start to include (~240ms)

	# End-of-speech: consecutive VAD-silent frames before stopping.
	# At 30ms/frame: 25 frames ≈ 750ms of silence.
	SILENCE_FRAMES_END = 25

	# Minimum speech frames before bothering to transcribe.
	MIN_SPEECH_FRAMES  = 4                                        # ~120ms

	# Safety net: maximum speech duration before forcing end.
	# At 30ms/frame: 333 frames ≈ 10 seconds.
	MAX_SPEECH_FRAMES  = 333

	# Maximum frames to wait for speech to begin before giving up.
	# At 30ms/frame: 333 frames ≈ 10 seconds.
	MAX_PRESPEECH_FRAMES = 333

	def __init__(self) -> None:
		self._server_proc   = None
		self._listen_thread = None
		self._listening     = False
		self._vad           = webrtcvad.Vad(self.VAD_AGGRESSIVENESS)
		self._alsa_device   = self._find_alsa_device()
		print(f"SpeechToText: using device {self._alsa_device}")

		self._start_whisper_server()
		print("SpeechToText: initialized.")

	def _find_alsa_device(self) -> str:
		result = subprocess.run(["arecord", "-l"], capture_output=True, text=True)
		for line in result.stdout.splitlines():
			if 'respeaker' in line.lower():
				card_num = line.split(":")[0].replace("card", "").strip()
				print(f"SpeechToText: found ReSpeaker at card {card_num}")
				return f"plughw:{card_num},0"
		print("SpeechToText: ReSpeaker not found!")
		return "plughw:0,0"

	def _whisper_lib_dirs(self) -> list:
		"""Find every directory under the whisper build tree that holds a .so.

		whisper-server links against libwhisper.so.1 and libggml.so.0, which
		the build leaves in subdirs of lib/whisper/build/ (e.g. build/src,
		build/ggml/src) rather than anywhere on the default linker path. We
		collect all of them so LD_LIBRARY_PATH covers wherever this particular
		build placed its shared objects.
		"""
		whisper_build = os.path.join(
			os.path.dirname(os.path.abspath(__file__)), "lib", "whisper", "build"
		)
		lib_dirs = set()
		for root, _dirs, files in os.walk(whisper_build):
			if any(f.endswith(".so") or ".so." in f for f in files):
				lib_dirs.add(root)
		return sorted(lib_dirs)

	def _start_whisper_server(self) -> None:
		"""Launch whisper-server and wait until it actually answers.

		Loading the ggml model on the Orin Nano takes noticeably longer than a
		fixed sleep can safely cover (base.en especially), so instead of
		sleeping a guessed interval we poll the port until the server responds
		or the process dies. This prevents the first transcription POST from
		racing an unready server (Connection refused).

		The server's shared libs (libwhisper.so.1, libggml.so.0) live in the
		build tree, not on the default linker path, so we pass an explicit
		LD_LIBRARY_PATH via env — this makes startup independent of .bashrc and
		survives being launched under sudo (which strips the user environment).
		"""
		# Kill any existing whisper-server processes first
		subprocess.run(["pkill", "-f", "whisper-server"], capture_output=True)
		time.sleep(1)

		# Prepend the whisper build lib dirs to LD_LIBRARY_PATH.
		env = os.environ.copy()
		lib_dirs = self._whisper_lib_dirs()
		if lib_dirs:
			existing = env.get("LD_LIBRARY_PATH", "")
			env["LD_LIBRARY_PATH"] = ":".join(lib_dirs + ([existing] if existing else []))
			print(f"SpeechToText: whisper LD_LIBRARY_PATH += {':'.join(lib_dirs)}")
		else:
			print("SpeechToText: WARNING — no whisper .so dirs found under build/; "
			      "libwhisper/libggml may fail to load.")

		print("SpeechToText: starting whisper-server...")
		self._server_proc = subprocess.Popen(
			[
				self.WHISPER_SERVER_BIN,
				"-m", self.WHISPER_MODEL,
				"--host", "127.0.0.1",
				"--port", "8080",
				# Beam search width — see BEAM_SIZE.
				"-bs", str(self.BEAM_SIZE),
				# Flash attention defaults ON in recent whisper.cpp and HANGS
				# GPU inference on the Orin (Ampere 8.7). Disable it — without
				# this the server accepts the request then never returns.
				"--no-flash-attn",
			],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			env=env,
		)

		if self._wait_for_whisper_ready(timeout=60.0):
			print("SpeechToText: whisper-server ready.")
		else:
			print("SpeechToText: WARNING — whisper-server did not become ready; "
			      "transcription will fail until it is up.")

	def _wait_for_whisper_ready(self, timeout: float = 60.0, interval: float = 0.5) -> bool:
		"""Poll the whisper-server until it responds or times out.

		Returns True once the HTTP endpoint is reachable. Returns False if the
		timeout elapses or the server process exits early (e.g. bad model path,
		port already bound, missing CUDA libs) — in which case _server_proc has
		a non-None poll() and we stop waiting immediately.
		"""
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			# If the process already exited, no point waiting for the port.
			if self._server_proc is not None and self._server_proc.poll() is not None:
				code = self._server_proc.returncode
				print(f"SpeechToText: whisper-server exited early (code {code}). "
				      f"Check model path '{self.WHISPER_MODEL}' and that port 8080 is free.")
				return False
			try:
				# A GET to the inference endpoint returns an error status (it
				# wants a POST), but ANY HTTP response means the server is up
				# and listening — which is all we need to know.
				requests.get("http://127.0.0.1:8080/", timeout=1.0)
				return True
			except requests.exceptions.RequestException:
				time.sleep(interval)
		return False

	def listen_once(self) -> None:
		if self._listening:
			if self._listen_thread and not self._listen_thread.is_alive():
				print("SpeechToText: thread dead but _listening stuck True, resetting.")
				self._listening = False
			else:
				print("SpeechToText: already listening, ignoring request.")
				return
			
		print(f"SpeechToText: listen_once called, _listening={self._listening}")
		self._listen_thread = threading.Thread(target=self._capture_and_transcribe, daemon=True)
		self._listen_thread.start()

	def _capture_and_transcribe(self) -> None:
		self._listening = True
		dispatcher.send(signal="updateStatus", id="Voice Command Status", value="Listening...")

		arecord_cmd = [
			"arecord",
			"-D", self._alsa_device,
			"-f", "S16_LE",
			"-r", str(self.SAMPLE_RATE),
			"-c", "2",            # stereo — ReSpeaker outputs beamformed audio on left channel
			"--buffer-size=4096",
			"-t", "raw",
		]

		proc = subprocess.Popen(arecord_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
		print(f"SpeechToText: arecord started, pid={proc.pid}")

		speech_frames    = []
		silence_count    = 0
		in_speech        = False
		preroll_buffer   = []
		leftover         = b""
		done             = False
		prespeech_frames = 0

		# Adaptive noise floor — rolling window of recent RMS values while not in speech
		noise_window   = deque(maxlen=self.NOISE_FLOOR_WINDOW)
		noise_floor    = self.NOISE_FLOOR_MIN

		try:
			while True:
				# Read one stereo VAD frame worth of data
				raw = proc.stdout.read(self.VAD_FRAME_BYTES * 2)  # *2 for stereo
				if not raw:
					break

				# Extract left channel (beamformed output)
				samples = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2)
				data = samples[:, 0].tobytes()

				# Prepend leftover bytes from last iteration
				data     = leftover + data
				leftover = b""

				# Process complete VAD frames
				while len(data) >= self.VAD_FRAME_BYTES:
					frame = data[:self.VAD_FRAME_BYTES]
					data  = data[self.VAD_FRAME_BYTES:]

					energy = self._rms(frame)

					if not in_speech:
						prespeech_frames += 1
						if prespeech_frames >= self.MAX_PRESPEECH_FRAMES:
							print(f"SpeechToText: no speech detected within timeout, giving up.")
							dispatcher.send(signal="transcriptionResult", text="[SILENCE]")
							done = True
							break

						# Update adaptive noise floor
						noise_window.append(energy)
						if len(noise_window) >= 10:  # need at least 10 frames before trusting the floor
							noise_floor = max(self.NOISE_FLOOR_MIN, float(np.mean(noise_window)))

						threshold = noise_floor * self.SPEECH_RATIO

						if energy > threshold:
							in_speech     = True
							silence_count = 0
							speech_frames = list(preroll_buffer)
							print(f"SpeechToText: speech started (energy={energy:.0f} threshold={threshold:.0f} floor={noise_floor:.0f}).")
						else:
							preroll_buffer.append(frame)
							if len(preroll_buffer) > self.PREROLL_FRAMES:
								preroll_buffer.pop(0)
					else:
						# Use webrtcvad to detect end-of-speech — smarter than RMS for this
						speech_frames.append(frame)
						try:
							vad_says_speech = self._vad.is_speech(frame, self.SAMPLE_RATE)
						except Exception:
							vad_says_speech = energy > noise_floor * self.SPEECH_RATIO

						if vad_says_speech:
							silence_count = 0
						else:
							silence_count += 1

						if silence_count >= self.SILENCE_FRAMES_END or len(speech_frames) >= self.MAX_SPEECH_FRAMES:
							if len(speech_frames) >= self.MAX_SPEECH_FRAMES:
								print(f"SpeechToText: max duration reached, forcing end.")
							else:
								print(f"SpeechToText: speech ended ({len(speech_frames)} frames, floor={noise_floor:.0f}).")
							if len(speech_frames) >= self.MIN_SPEECH_FRAMES:
								text = self._transcribe(speech_frames)
								if text:
									print(f"SpeechToText: transcribed: {text}")
							else:
								text = ""
							# Always dispatch so caller can clean up, even if transcription was empty
							dispatcher.send(signal="transcriptionResult", text=text)
							done = True
							break

				if done:
					break

				leftover = data

		finally:
			# terminate() only signals; wait() ensures arecord has actually
			# exited and released the ALSA capture device before we return, so
			# the wakeword's arecord can open the mic cleanly next.
			try:
				proc.terminate()
				proc.wait(timeout=3)
			except Exception:
				try:
					proc.kill()
				except Exception:
					pass
			self._listening = False

	def _rms(self, data: bytes) -> float:
		samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
		return np.sqrt(np.mean(samples**2))

	def _transcribe(self, frames: list) -> str:
		audio = np.frombuffer(b"".join(frames), dtype=np.int16)

		with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
			tmp_path = f.name
		wf = wave.open(tmp_path, "wb")
		wf.setnchannels(1)
		wf.setsampwidth(2)
		wf.setframerate(self.SAMPLE_RATE)
		wf.writeframes(audio.tobytes())
		wf.close()

		try:
			with open(tmp_path, "rb") as f:
				response = requests.post(
					self.WHISPER_URL,
					files={"file": ("audio.wav", f, "audio/wav")},
					# temperature is deliberately omitted: some builds treat an
					# explicit 0 as a request for greedy decoding, which would
					# silently override the beam width set at server launch.
					data={"response_format": "json"},
					timeout=30.0,
				)
			if response.ok:
				text = response.json().get("text", "").strip()
				# Whisper sometimes returns this literal string for silence
				if text.upper() == "[BLANK_AUDIO]":
					return ""
				print(f"SpeechToText: transcribed: {text!r}")
				return text
			else:
				print(f"SpeechToText: whisper returned HTTP {response.status_code}: "
				      f"{response.text[:200]}")
		except requests.exceptions.Timeout:
			print("SpeechToText: transcription timed out (whisper-server slow or stuck). "
			      "First GPU inference can be slow; if this recurs, check server load.")
		except Exception as e:
			print(f"SpeechToText: transcription error: {e}")
		finally:
			os.unlink(tmp_path)

		return ""

	def shutdown(self) -> None:
		"""Stop listening and kill the whisper-server process."""
		self._listening = False
		if self._server_proc:
			self._server_proc.terminate()
			self._server_proc = None
		print("SpeechToText: shutdown complete.")
