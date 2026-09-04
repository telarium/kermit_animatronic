#!/usr/bin/env python3
import os
import queue
import re
import threading
import time
import numpy as np
import sherpa_onnx
import audio_setup
from pydispatch import dispatcher


class SpeechToText:
	"""Streaming speech-to-text via sherpa-onnx + Nemotron streaming transducer.

	End-of-speech is decided by the acoustic model rather than by audio levels:
	the recognizer reports an endpoint when the decoder stops emitting tokens.
	Background noise produces no tokens, so a noisy room no longer holds the
	utterance open the way an RMS threshold did.

	Audio comes from the shared capture stream in audio_setup rather than a
	private arecord process. That removes the mic handoff between the wakeword
	and STT, which was silently dropping whatever was said during the changeover.
	"""

	MODEL_DIR = os.path.join(
		os.path.dirname(os.path.abspath(__file__)),
		"lib/sherpa_onnx/models/sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25",
	)

	SAMPLE_RATE = audio_setup.MIC_RATE

	# Orin Nano has 6 Cortex-A78AE cores. 4 leaves headroom for the wakeword
	# session, the web server, and the animation/movement threads.
	NUM_THREADS = 4

	# int8-quantized ONNX is a CPU-side optimization — the quantized ops do not
	# map onto the CUDA execution provider, so requesting "cuda" would silently
	# fall back to CPU for most of the graph while adding onnxruntime-gpu setup
	# pain for no gain.
	PROVIDER = "cpu"

	# --- Endpoint rules ------------------------------------------------------
	# "Trailing silence" here means "the decoder emitted no tokens", not "the
	# signal went quiet", which is the whole point of the streaming model.
	#
	# rule1 (trailing silence with NO speech required) is deliberately pushed
	# out of reach. Its job was detecting "the user said nothing", but
	# PRESPEECH_TIMEOUT_S already does that without side effects. Left at its
	# default 2.4s it fired while waiting for the user to start, and the
	# reset() that followed discarded decoder state mid-word — swallowing
	# short commands like "stop" that were still in flight. Dead air is now
	# handled purely by the deadline.
	RULE1_MIN_TRAILING_SILENCE = 300.0
	# rule2 is the real end-of-utterance: speech happened, then stopped.
	RULE2_MIN_TRAILING_SILENCE = 1.2
	# rule3 caps a single utterance.
	RULE3_MIN_UTTERANCE_LENGTH = 15.0

	# How long to wait for the user to start talking at all before reporting
	# [SILENCE].
	PRESPEECH_TIMEOUT_S = 10.0

	# Upper bound on recovered pre-listen audio. Long enough to cover the
	# wakeword animation and handler startup, short enough that it can never
	# reach back into whatever was playing before this turn.
	MAX_PREROLL_SECONDS = 2.0

	# Reach back before the anchor to cover openwakeword's confirmation delay.
	# A wakeword tail that leaks in is handled by _strip_wakeword; a clipped
	# command is not recoverable. The mic ring is 4s, so this fits comfortably.
	ANCHOR_LEAD_SECONDS = 0.5

	# Leading wakeword fragments to strip from a transcript. openwakeword
	# normally fires at the end of the phrase, but it can trigger a beat early
	# and leave part of it in the preroll.
	WAKEWORD_PREFIXES = (
		r"okay,?\s+kermit",
		r"ok,?\s+kermit",
		r"hey,?\s+kermit",
		r"kermit",
	)

	def __init__(self) -> None:
		self._listen_thread = None
		self._listening = False

		self._recognizer = self._build_recognizer()
		self._warm_up()
		print("SpeechToText: initialized.")

	# -------------------------------------------------------------------------
	# Setup
	# -------------------------------------------------------------------------

	def _build_recognizer(self) -> sherpa_onnx.OnlineRecognizer:
		"""Load the streaming transducer.

		Takes a few seconds (the int8 encoder is ~623MB) and happens once at
		startup rather than per-utterance.
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
			# Without this the encoder's cache tensors are wired up as if it
			# were a zipformer2 and decoding produces garbage.
			model_type="nemotron",
			# Nemotron only supports greedy_search in sherpa-onnx today;
			# modified_beam_search (and therefore hotwords) is not wired up
			# for this model type.
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

		onnxruntime allocates lazily on first inference; without this the start
		of the first utterance competes with arena setup.
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

		q = None
		text = ""
		try:
			# Subscribe BEFORE draining the ring buffer, so audio arriving
			# between the two calls lands in the queue rather than falling
			# down the gap between them.
			q = audio_setup.subscribe_mic()

			stream = self._recognizer.create_stream()
			deadline = time.monotonic() + self.PRESPEECH_TIMEOUT_S
			heard_speech = False
			chunks_seen = 0

			# Audio captured since the turn's anchor — whatever was said while
			# the acknowledgement animation played and this handler was
			# starting up. This is what recovers the missing first word, and
			# it warms the encoder's left-context cache. Capped so a stale or
			# unreset anchor can't drag in a long stretch of history.
			preroll = audio_setup.mic_audio_since_anchor(
				self.MAX_PREROLL_SECONDS, self.ANCHOR_LEAD_SECONDS
			)
			if preroll.size:
				print(f"SpeechToText: priming with "
				      f"{preroll.size / self.SAMPLE_RATE:.2f}s of preroll "
				      f"(level {int(np.abs(preroll).mean())}).")
				stream.accept_waveform(
					self.SAMPLE_RATE, preroll.astype(np.float32) / 32768.0
				)
				while self._recognizer.is_ready(stream):
					self._recognizer.decode_stream(stream)

			while True:
				try:
					chunk = q.get(timeout=0.5)
				except queue.Empty:
					# Timeout rather than block forever, so the pre-speech
					# deadline is still enforced if capture stalls.
					if not heard_speech and time.monotonic() > deadline:
						print("SpeechToText: no speech detected within timeout, giving up.")
						break
					continue

				chunks_seen += 1
				if chunks_seen % 25 == 0 and not heard_speech:
					# Diagnostic only — nothing branches on this. It separates
					# "no audio is arriving" from "audio is arriving but the
					# model isn't emitting tokens", which need opposite fixes.
					level = float(np.abs(chunk.astype(np.float32)).mean())
					print(f"SpeechToText: waiting... {chunks_seen} chunks "
					      f"({chunks_seen * 0.08:.1f}s), mean level {level:.0f}, "
					      f"no tokens yet.")

				stream.accept_waveform(
					self.SAMPLE_RATE, chunk.astype(np.float32) / 32768.0
				)
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
					cleaned = self._strip_wakeword(partial)
					if cleaned:
						text = cleaned
						print(f"SpeechToText: endpoint reached: {text!r}")
						break

					# Nothing usable in this segment. Two ways to get here:
					# rule1 firing on dead air, or an endpoint triggered by the
					# preroll's own trailing silence when the only thing in it
					# was the tail of the wakeword. In both cases the user
					# hasn't said their command yet, so reset and keep
					# listening instead of ending the turn on it.
					if partial:
						print(f"SpeechToText: discarding wakeword-only segment "
						      f"{partial!r}, still listening.")
					self._recognizer.reset(stream)
					heard_speech = False
					deadline = time.monotonic() + self.PRESPEECH_TIMEOUT_S

				if not heard_speech and time.monotonic() > deadline:
					print("SpeechToText: no speech detected within timeout, giving up.")
					break

		except Exception as e:
			print(f"SpeechToText: capture/decode error: {e}")
		finally:
			if q is not None:
				audio_setup.unsubscribe_mic(q)
			self._listening = False

		# text is already stripped at the endpoint check above — deliberately
		# not stripped again here, so a command that legitimately contains the
		# name ("tell Kermit a joke") keeps it.
		#
		# start.py treats "" and "[SILENCE]" differently from real text, and
		# llm_service.py's CONTEXT_POSTFIX has explicit handling for
		# [SILENCE], so preserve that contract exactly.
		dispatcher.send(
			signal="transcriptionResult", text=text if text else "[SILENCE]"
		)

	def _strip_wakeword(self, text: str) -> str:
		"""Remove a leading wakeword phrase from the transcript.

		The anchor is set when openwakeword fires, normally at or just after
		the end of the phrase — but it can trigger slightly early, leaving a
		fragment of "Kermit" at the head of the preroll. Left in place that
		fragment breaks VoiceCommandHandler, whose play-by-name match requires
		the transcript to START with a play prefix.
		"""
		if not text:
			return text
		for phrase in self.WAKEWORD_PREFIXES:
			new = re.sub(
				r"^\s*" + phrase + r"\b[\s,.!-]*", "", text,
				count=1, flags=re.IGNORECASE,
			)
			if new != text:
				print(f"SpeechToText: stripped wakeword fragment from {text!r}")
				return new.strip()
		return text.strip()
