#!/usr/bin/env python3
import os
import subprocess
import threading
import time
import numpy as np
import sherpa_onnx
from pydispatch import dispatcher


class SpeechToText:
	"""Streaming speech-to-text via sherpa-onnx + Nemotron streaming transducer.

	Replaces the previous whisper.cpp implementation. The important
	architectural difference: this is a *streaming* recognizer, so
	end-of-speech is decided by the acoustic model rather than by audio
	levels. The recognizer reports an endpoint when the decoder stops
	emitting tokens — background noise produces no tokens, so a noisy room
	no longer holds the utterance open the way an RMS threshold did.

	Consequently the old machinery is gone entirely: no adaptive noise
	floor, no SPEECH_RATIO, no webrtcvad, no preroll buffer, no
	whisper-server subprocess, no WAV temp files, no HTTP round trip.
	"""

	MODEL_DIR = os.path.join(
		os.path.dirname(os.path.abspath(__file__)),
		"lib/sherpa_onnx/models/sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25",
	)

	SAMPLE_RATE = 16000

	# Orin Nano has 6 Cortex-A78AE cores. 4 leaves headroom for the wakeword
	# session, the web server, and the animation/movement threads. Raise to 5
	# only if measured RTF is uncomfortably close to 1.0.
	NUM_THREADS = 4

	# int8-quantized ONNX is a CPU-side optimization — the quantized ops do
	# not map onto the CUDA execution provider, so requesting "cuda" here
	# would silently fall back to CPU for most of the graph while adding
	# onnxruntime-gpu setup pain for no gain. Keep this on cpu.
	PROVIDER = "cpu"

	# --- Endpoint rules ------------------------------------------------------
	# These are what replace SILENCE_FRAMES_END / THRESHOLD. "Trailing
	# silence" here means "the decoder emitted no tokens", not "the signal
	# went quiet", which is the whole point of the change.
	#
	# rule1 fires on trailing silence with NO speech required — i.e. the user
	# never said anything. We use it to detect a dead-air turn.
	# rule2 is the normal end-of-utterance: speech happened, then stopped.
	# rule3 is the hard cap on a single utterance (the old MAX_SPEECH_FRAMES).
	RULE1_MIN_TRAILING_SILENCE = 2.4
	RULE2_MIN_TRAILING_SILENCE = 1.2
	RULE3_MIN_UTTERANCE_LENGTH = 15.0

	# How long to wait for the user to start talking at all before giving up
	# and reporting [SILENCE]. Replaces MAX_PRESPEECH_FRAMES.
	PRESPEECH_TIMEOUT_S = 10.0

	# Audio read granularity. 100ms keeps the decode loop responsive without
	# thrashing on tiny reads.
	READ_CHUNK_MS = 100
	READ_CHUNK_SAMPLES = int(SAMPLE_RATE * READ_CHUNK_MS / 1000)

	# ReSpeaker outputs beamformed audio on the left channel of a stereo stream.
	CHANNELS = 2

	def __init__(self) -> None:
		self._listen_thread = None
		self._listening = False
		self._alsa_device = self._find_alsa_device()
		print(f"SpeechToText: using device {self._alsa_device}")

		self._recognizer = self._build_recognizer()
		self._warm_up()
		print("SpeechToText: initialized.")

	# -------------------------------------------------------------------------
	# Setup
	# -------------------------------------------------------------------------

	def _find_alsa_device(self) -> str:
		result = subprocess.run(["arecord", "-l"], capture_output=True, text=True)
		for line in result.stdout.splitlines():
			if 'respeaker' in line.lower():
				card_num = line.split(":")[0].replace("card", "").strip()
				print(f"SpeechToText: found ReSpeaker at card {card_num}")
				return f"plughw:{card_num},0"
		print("SpeechToText: ReSpeaker not found!")
		return "plughw:0,0"

	def _build_recognizer(self) -> sherpa_onnx.OnlineRecognizer:
		"""Load the streaming transducer.

		This takes a few seconds (the int8 encoder is ~623MB) and happens once
		at startup rather than per-utterance, which is why there is no
		equivalent of the old _wait_for_whisper_ready polling here — by the
		time __init__ returns, the model is resident and usable.
		"""
		encoder = os.path.join(self.MODEL_DIR, "encoder.int8.onnx")
		decoder = os.path.join(self.MODEL_DIR, "decoder.int8.onnx")
		joiner = os.path.join(self.MODEL_DIR, "joiner.int8.onnx")
		tokens = os.path.join(self.MODEL_DIR, "tokens.txt")

		for path in (encoder, decoder, joiner, tokens):
			if not os.path.exists(path):
				raise FileNotFoundError(
					f"SpeechToText: missing model file '{path}'. "
					f"Run setup.py, or download the model package into {self.MODEL_DIR}."
				)

		print("SpeechToText: loading Nemotron streaming model...")
		t0 = time.monotonic()
		recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
			tokens=tokens,
			encoder=encoder,
			decoder=decoder,
			joiner=joiner,
			num_threads=self.NUM_THREADS,
			sample_rate=self.SAMPLE_RATE,
			feature_dim=80,
			provider=self.PROVIDER,
			# sherpa-onnx needs to be told this is a Nemotron cache-aware
			# transducer; without it the encoder's cache tensors are wired
			# up as if it were a zipformer2 and decoding produces garbage.
			model_type="nemotron",
			# Nemotron only supports greedy_search in sherpa-onnx today.
			# modified_beam_search (and therefore hotwords) is not wired up
			# for this model type — passing it raises rather than silently
			# degrading, so this is explicit on purpose.
			decoding_method="greedy_search",
			enable_endpoint_detection=True,
			rule1_min_trailing_silence=self.RULE1_MIN_TRAILING_SILENCE,
			rule2_min_trailing_silence=self.RULE2_MIN_TRAILING_SILENCE,
			rule3_min_utterance_length=self.RULE3_MIN_UTTERANCE_LENGTH,
		)
		print(f"SpeechToText: model loaded in {time.monotonic() - t0:.1f}s.")
		return recognizer

	def _warm_up(self) -> None:
		"""Push a little silence through so the first real utterance isn't slow.

		onnxruntime does lazy allocation on first inference; without this the
		first ~200ms of the user's first sentence competes with arena setup
		and can be dropped or decoded poorly.
		"""
		stream = self._recognizer.create_stream()
		stream.accept_waveform(
			self.SAMPLE_RATE, np.zeros(self.SAMPLE_RATE, dtype=np.float32)
		)
		stream.input_finished()
		while self._recognizer.is_ready(stream):
			self._recognizer.decode_stream(stream)
		print("SpeechToText: warm-up complete.")

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

	def listen_once(self) -> None:
		if self._listening:
			if self._listen_thread and not self._listen_thread.is_alive():
				print("SpeechToText: thread dead but _listening stuck True, resetting.")
				self._listening = False
			else:
				print("SpeechToText: already listening, ignoring request.")
				return

		print(f"SpeechToText: listen_once called, _listening={self._listening}")
		self._listen_thread = threading.Thread(
			target=self._capture_and_transcribe, daemon=True
		)
		self._listen_thread.start()

	def shutdown(self) -> None:
		self._listening = False
		print("SpeechToText: shutdown complete.")

	# -------------------------------------------------------------------------
	# Internal
	# -------------------------------------------------------------------------

	def _capture_and_transcribe(self) -> None:
		self._listening = True
		dispatcher.send(
			signal="updateStatus", id="Voice Command Status", value="Listening..."
		)

		arecord_cmd = [
			"arecord",
			"-D", self._alsa_device,
			"-f", "S16_LE",
			"-r", str(self.SAMPLE_RATE),
			"-c", str(self.CHANNELS),
			"--buffer-size=4096",
			"-t", "raw",
		]

		proc = subprocess.Popen(
			arecord_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
		)
		print(f"SpeechToText: arecord started, pid={proc.pid}")

		stream = self._recognizer.create_stream()
		chunk_bytes = self.READ_CHUNK_SAMPLES * 2 * self.CHANNELS
		deadline = time.monotonic() + self.PRESPEECH_TIMEOUT_S
		text = ""
		heard_speech = False

		try:
			while True:
				raw = proc.stdout.read(chunk_bytes)
				if not raw or len(raw) < chunk_bytes:
					print("SpeechToText: capture stream ended unexpectedly.")
					break

				# Take the left channel (ReSpeaker beamformed output) and
				# convert to the float32 in [-1, 1] that sherpa-onnx expects.
				samples = np.frombuffer(raw, dtype=np.int16).reshape(-1, self.CHANNELS)
				mono = samples[:, 0].astype(np.float32) / 32768.0

				stream.accept_waveform(self.SAMPLE_RATE, mono)
				while self._recognizer.is_ready(stream):
					self._recognizer.decode_stream(stream)

				partial = self._recognizer.get_result(stream).strip()

				# The first time tokens appear, the user has started talking.
				# From here the pre-speech timeout no longer applies — rule2
				# and rule3 own the rest of the turn.
				if partial and not heard_speech:
					heard_speech = True
					print(f"SpeechToText: speech started ({partial!r}).")

				if self._recognizer.is_endpoint(stream):
					if partial:
						text = partial
						print(f"SpeechToText: endpoint reached: {text!r}")
						break
					# Endpoint with no text is rule1 firing on dead air. Reset
					# and keep waiting until the pre-speech deadline, rather
					# than reporting silence after only 2.4s.
					self._recognizer.reset(stream)

				if not heard_speech and time.monotonic() > deadline:
					print("SpeechToText: no speech detected within timeout, giving up.")
					break

			# If we fell out of the loop mid-utterance (arecord died), flush
			# whatever the decoder still holds rather than discarding it.
			if not text and heard_speech:
				stream.input_finished()
				while self._recognizer.is_ready(stream):
					self._recognizer.decode_stream(stream)
				text = self._recognizer.get_result(stream).strip()

		except Exception as e:
			print(f"SpeechToText: capture/decode error: {e}")
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

		# start.py treats "" and "[SILENCE]" differently from real text, and
		# llm_service.py's CONTEXT_POSTFIX has explicit handling for
		# [SILENCE], so preserve that contract exactly.
		dispatcher.send(
			signal="transcriptionResult", text=text if text else "[SILENCE]"
		)