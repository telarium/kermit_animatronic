#!/usr/bin/env python3
import configparser
import glob
import io
import json
import os
import re
import tempfile
import threading
import time
import wave
import requests
from pydispatch import dispatcher
from typing import List, Optional

ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1/text-to-speech"

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Sanitization applied to every line before synthesis, on both the cloud and
# offline paths. Raw text still reaches the logs and the web UI upstream of
# here — this is only what the voice actually says.
_STAGE_DIRECTIONS_RE = re.compile(r"\*[^*]*\*")
# Folded rather than dropped: deleting a curly apostrophe turns "don't" into
# "dont", which Piper duly pronounces.
_UNICODE_FOLD = {
	"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
	"\u201c": '"', "\u201d": '"', "\u201e": '"',
	"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
	"\u2014": " - ", "\u2015": " - ",
	"\u2026": "...", "\u00a0": " ",
	# Expanded rather than dropped — the offline path never sees the
	# CONTEXT_POSTFIX instruction to spell these out.
	"%": " percent ", "&": " and ", "+": " plus ",
}
_DISALLOWED_RE = re.compile(r"[^A-Za-z0-9\s.,!?'\";:()\-]")
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_for_speech(text: str) -> str:
	"""Reduce a reply to what a voice can actually pronounce."""
	if not text:
		return ""
	text = _STAGE_DIRECTIONS_RE.sub(" ", text)
	for source, replacement in _UNICODE_FOLD.items():
		text = text.replace(source, replacement)
	text = _DISALLOWED_RE.sub("", text)
	return _WHITESPACE_RE.sub(" ", text).strip()


class TextToSpeech:
	"""ElevenLabs first, local Piper voice as the fallback.

	Anything that stops ElevenLabs returning audio — no key, no network, an
	API error — drops through to the Piper voice named by the "piper" block
	in the character JSON, so the animatronic still speaks offline.
	"""

	def __init__(self, hardware_path: Optional[str] = None) -> None:
		self.elevenlabs_key: str = ""
		self.elevenlabs_voice_id: str = ""
		self.elevenlabs_stability: float = 0.35
		self.elevenlabs_similarity_boost: float = 0.75
		self.elevenlabs_style: float = 0.3
		self.elevenlabs_use_high_quality_slow_model: bool = False

		# Offline fallback voice.
		self.piper_dir: str = ""
		self.piper_speed: float = 1.0
		self.piper_volume: float = 1.0
		self._piper_voice = None
		self._piper_lock = threading.Lock()
		self._warned_legacy_volume: bool = False
		self.apply_hardware_config(hardware_path)

		print("Set up TextToSpeech")
		self.warm_up()

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

	def apply_config(self, path: str) -> None:
		config = configparser.ConfigParser()
		try:
			config.read(path)
		except configparser.Error as e:
			print(f"TextToSpeech: failed to parse config at '{path}': {e}")
			return

		self.elevenlabs_key      = config.get("TextToSpeech", "ElevenLabsKey",     fallback="").strip()
		self.elevenlabs_voice_id = config.get("TextToSpeech", "ElevenLabsVoiceID", fallback="").strip()
		self.elevenlabs_stability             = config.getfloat("TextToSpeech", "ElevenLabsStability",              fallback=0.35)
		self.elevenlabs_similarity_boost      = config.getfloat("TextToSpeech", "ElevenLabsSimularityBoost",        fallback=0.75)
		self.elevenlabs_style                 = config.getfloat("TextToSpeech", "ElevenLabsStyle",                  fallback=0.3)
		self.elevenlabs_use_high_quality_slow_model = config.getboolean("TextToSpeech", "ElevenLabsUseHighQualitySlowModel", fallback=False)
		print(f"TextToSpeech: voice settings applied (stability={self.elevenlabs_stability}, similarity_boost={self.elevenlabs_similarity_boost}, style={self.elevenlabs_style}, high_quality={self.elevenlabs_use_high_quality_slow_model})")

	def apply_hardware_config(self, hardware_path: Optional[str]) -> None:
		"""Read the optional "piper" block from the character JSON:

			"piper": { "directory": "lib/piper/kermit/", "speed": 1.3, "volume": 0.8 }
		"""
		if not hardware_path:
			return
		try:
			with open(hardware_path, 'r') as f:
				config = json.load(f)
		except (OSError, ValueError) as e:
			print(f"TextToSpeech: could not read character config '{hardware_path}': {e}")
			return

		piper = config.get('piper', {})
		if not isinstance(piper, dict):
			print("TextToSpeech: 'piper' block is not an object, ignoring.")
			return

		directory = str(piper.get('directory', '')).strip()
		self.piper_dir = os.path.join(_BASE_DIR, directory) if directory else ""

		# speed is Piper's length_scale: >1 is slower.
		self.piper_speed  = self._positive_float(piper, 'speed', self.piper_speed)
		# volume scales the normalized output: 0.8 is 80% of full scale.
		self.piper_volume = self._positive_float(piper, 'volume', self.piper_volume, maximum=1.0)

		if self.piper_dir:
			print(f"TextToSpeech: offline voice dir '{self.piper_dir}' "
			      f"(speed={self.piper_speed}, volume={self.piper_volume})")

	@staticmethod
	def _positive_float(block: dict, name: str, default: float, maximum: Optional[float] = None) -> float:
		raw = block.get(name, default)
		if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw <= 0:
			print(f"TextToSpeech: piper.{name} must be a positive number, ignoring '{raw}'")
			return default
		if maximum is not None and raw > maximum:
			print(f"TextToSpeech: piper.{name} clamped to {maximum} (was {raw})")
			return maximum
		return float(raw)

	def speak(self, text: str, bForceOffline: bool = False) -> None:
		"""Convert text to speech asynchronously. bForceOffline skips
		ElevenLabs entirely and goes straight to the Piper voice."""
		threading.Thread(target=self._speak, args=(text, bForceOffline), daemon=True).start()

	def warm_up(self) -> None:
		"""Load the Piper voice and push a short phrase through it, so the
		first offline response isn't stalled by the model load and
		onnxruntime's lazy allocation. Runs on a background thread — startup
		must not block on it, and the voice lock makes an early speak() safe.
		"""
		if not self.piper_dir:
			return
		threading.Thread(target=self._warm_up, daemon=True).start()

	# -------------------------------------------------------------------------
	# Internal
	# -------------------------------------------------------------------------

	def _warm_up(self) -> None:
		t0 = time.monotonic()
		voice = self._load_piper_voice()
		if voice is None:
			return

		tmp_path = None
		try:
			tmp = tempfile.NamedTemporaryFile(
				suffix=".wav", delete=False, prefix="kermit_tts_warm_"
			)
			tmp.close()
			tmp_path = tmp.name
			# Synthesized and thrown away — never dispatched for playback.
			with wave.open(tmp_path, "wb") as wav_file:
				self._synthesize_piper(voice, "Hi ho.", wav_file)
			print(f"TextToSpeech: offline voice warm-up complete in {time.monotonic() - t0:.1f}s.")
		except Exception as e:
			print(f"TextToSpeech: offline voice warm-up failed: {e}")
		finally:
			if tmp_path:
				try:
					os.remove(tmp_path)
				except OSError:
					pass

	def _speak(self, text: str, bForceOffline: bool = False) -> None:
		raw = text
		text = sanitize_for_speech(text)
		if text != raw:
			print(f"TextToSpeech: sanitized -> {text!r}")
		if not text:
			print("TextToSpeech: nothing speakable left after sanitizing.")
			return
		started = time.monotonic()
		if not bForceOffline:
			if self._speak_elevenlabs(text):
				print(f"TextToSpeech: synthesis took {time.monotonic() - started:.1f}s.")
				return
			print("TextToSpeech: falling back to the offline voice.")
		if not self._speak_piper(text):
			print("TextToSpeech: no voice available — nothing spoken.")
			return
		print(f"TextToSpeech: synthesis took {time.monotonic() - started:.1f}s.")

	def _speak_elevenlabs(self, text: str) -> bool:
		"""Returns True only if audio was produced and dispatched."""
		if not self.elevenlabs_key:
			print("TextToSpeech: no ElevenLabs API key set.")
			return False
		if not self.elevenlabs_voice_id:
			print("TextToSpeech: no ElevenLabs voice ID set.")
			return False

		try:
			response = requests.post(
				f"{ELEVENLABS_API_URL}/{self.elevenlabs_voice_id}",
				headers={
					"xi-api-key": self.elevenlabs_key,
					"Content-Type": "application/json",
				},
				json={
				"text": text,
				"model_id": "eleven_turbo_v2_5" if not self.elevenlabs_use_high_quality_slow_model else "eleven_multilingual_v2",
				"voice_settings": {
					"stability": self.elevenlabs_stability,
					"similarity_boost": self.elevenlabs_similarity_boost,
					"style": self.elevenlabs_style,
					"use_speaker_boost": True,
				},
			},
				timeout=30,
			)
			response.raise_for_status()

			tmp = tempfile.NamedTemporaryFile(
				suffix=".mp3", delete=False, prefix="kermit_tts_"
			)
			tmp.write(response.content)
			tmp.close()

			dispatcher.send(signal="playVoiceFile", file=tmp.name)

			print(f"TextToSpeech: audio saved to {tmp.name}")
			return True

		except requests.HTTPError as e:
			print(f"TextToSpeech: HTTP error from ElevenLabs: {e}")
		except Exception as e:
			print(f"TextToSpeech: request failed: {e}")
		return False

	def _find_piper_files(self) -> Optional[tuple]:
		"""Locate (model.onnx, voice.json) in the configured directory."""
		if not self.piper_dir:
			print("TextToSpeech: no 'piper' directory in the character config.")
			return None
		if not os.path.isdir(self.piper_dir):
			print(f"TextToSpeech: Piper directory '{self.piper_dir}' not found.")
			return None

		models = sorted(glob.glob(os.path.join(self.piper_dir, "*.onnx")))
		if not models:
			print(f"TextToSpeech: no .onnx voice in '{self.piper_dir}'.")
			return None
		model = models[0]

		# Piper's convention is <model>.onnx.json; fall back to any .json.
		config = model + ".json"
		if not os.path.isfile(config):
			candidates = sorted(glob.glob(os.path.join(self.piper_dir, "*.json")))
			if not candidates:
				print(f"TextToSpeech: no voice .json alongside '{model}'.")
				return None
			config = candidates[0]

		return model, config

	def _load_piper_voice(self):
		"""Load the voice once and keep it — loading the model is slow."""
		with self._piper_lock:
			if self._piper_voice is not None:
				return self._piper_voice

			found = self._find_piper_files()
			if found is None:
				return None
			model, config = found

			try:
				try:
					from piper import PiperVoice
				except ImportError:
					from piper.voice import PiperVoice
			except ImportError as e:
				print(f"TextToSpeech: piper-tts not installed ({e}). Run setup.py.")
				return None

			try:
				self._piper_voice = PiperVoice.load(model, config_path=config)
			except Exception as e:
				print(f"TextToSpeech: failed to load Piper voice '{model}': {e}")
				return None

			print(f"TextToSpeech: loaded offline voice {os.path.basename(model)}")
			return self._piper_voice

	# Silence inserted at each "..." on the offline path. One wav rather than a
	# sequence of them: _calculate_rms normalizes against the peak of whatever
	# file it is given, so splitting would rescale a quiet trailing phrase up
	# to full mouth travel and lose the dynamics the pause is there to create.
	PIPER_ELLIPSIS_PAUSE_S = 0.4

	_ELLIPSIS_SPLIT_RE = re.compile(r"(?<=\.\.\.)(?!\.)\s*")
	_HAS_SPEECH_RE = re.compile(r"[A-Za-z0-9]")

	@classmethod
	def _split_on_ellipses(cls, text: str) -> List[str]:
		"""Split into chunks, keeping the '...' on the chunk it follows."""
		chunks = cls._ELLIPSIS_SPLIT_RE.split(text)
		return [c.strip() for c in chunks if cls._HAS_SPEECH_RE.search(c)]

	def _synthesize_piper_spaced(self, voice, text: str, wav_file) -> None:
		"""Synthesize each chunk and join them with real silence."""
		chunks = self._split_on_ellipses(text)
		if len(chunks) < 2:
			self._synthesize_piper(voice, text, wav_file)
			return

		params = None
		segments = []
		for chunk in chunks:
			buffer = io.BytesIO()
			with wave.open(buffer, "wb") as chunk_wav:
				self._synthesize_piper(voice, chunk, chunk_wav)
			buffer.seek(0)
			with wave.open(buffer, "rb") as chunk_wav:
				if params is None:
					params = chunk_wav.getparams()
				segments.append(chunk_wav.readframes(chunk_wav.getnframes()))

		if params is None:
			self._synthesize_piper(voice, text, wav_file)
			return

		wav_file.setnchannels(params.nchannels)
		wav_file.setsampwidth(params.sampwidth)
		wav_file.setframerate(params.framerate)

		pause_frames = int(params.framerate * self.PIPER_ELLIPSIS_PAUSE_S)
		silence = b"\x00" * (pause_frames * params.sampwidth * params.nchannels)
		for index, segment in enumerate(segments):
			if index:
				wav_file.writeframes(silence)
			wav_file.writeframes(segment)
		print(f"TextToSpeech: {len(segments)} chunks joined with "
		      f"{self.PIPER_ELLIPSIS_PAUSE_S:.2f}s pauses.")

	def _synthesize_piper(self, voice, text: str, wav_file) -> None:
		"""piper-tts >= 1.3 takes a SynthesisConfig; older releases take kwargs."""
		syn_config = None
		try:
			from piper import SynthesisConfig
			# normalize_audio (default True) peak-normalizes first, so volume
			# is a predictable fraction of full scale rather than of whatever
			# level this particular line happened to come out at.
			syn_config = SynthesisConfig(
				length_scale=self.piper_speed,
				volume=self.piper_volume,
			)
		except ImportError:
			pass

		if syn_config is not None and hasattr(voice, "synthesize_wav"):
			voice.synthesize_wav(text, wav_file, syn_config=syn_config)
			return

		# Legacy piper-tts has no volume control.
		if self.piper_volume != 1.0 and not self._warned_legacy_volume:
			self._warned_legacy_volume = True
			print("TextToSpeech: installed piper-tts is too old for piper.volume — ignoring it.")
		voice.synthesize(text, wav_file, length_scale=self.piper_speed)

	def _speak_piper(self, text: str) -> bool:
		"""Returns True only if audio was produced and dispatched."""
		voice = self._load_piper_voice()
		if voice is None:
			return False

		try:
			tmp = tempfile.NamedTemporaryFile(
				suffix=".wav", delete=False, prefix="kermit_tts_"
			)
			tmp.close()
			with wave.open(tmp.name, "wb") as wav_file:
				self._synthesize_piper_spaced(voice, text, wav_file)

			dispatcher.send(signal="playVoiceFile", file=tmp.name)

			print(f"TextToSpeech: offline audio saved to {tmp.name}")
			return True
		except Exception as e:
			print(f"TextToSpeech: Piper synthesis failed: {e}")
			return False
