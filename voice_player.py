from pydub import AudioSegment
from pydispatch import dispatcher
from scipy.io import wavfile
import pygame
import numpy as np
import time
import math
import io
import json
import os
import threading
import audio_setup
from typing import List, Optional, Any

USB_VOICES_DIR = "/mnt/usb/voices"

class _SyncChannel:
	"""One movement puppeted by the audio envelope.

	Each channel keeps its own envelope and hold state, so the mouth can track
	individual syllables while the head moves only on stressed, sustained
	passages. A solenoid is binary, so "moves less" means fewer and slower
	movements — a higher open_threshold and longer holds — not shorter travel.

	open_threshold   normalised RMS at which the movement fires
	close_threshold  level at which it releases; must be BELOW open_threshold
	                 or it chatters at the boundary
	attack_ms        envelope rise time. Short (~10ms) keeps syllable onsets
	                 crisp; long values smooth quiet onsets away entirely.
	release_ms       envelope fall time. Too long (>~60ms) merges adjacent
	                 syllables into one long movement.
	min_open_ms      floor on how long it stays open once opened. Must exceed
	                 the solenoid's dead time or you get clicks, not motion.
	min_closed_ms    floor on how long it stays closed
	interval_ms      analysis window, shared across all channels

	min_open_ms + min_closed_ms bounds the valve cycle rate to 1000 / (sum) Hz
	regardless of what the audio does.
	"""

	DEFAULTS = {
		"open_threshold":  0.07,
		"close_threshold": 0.06,
		"attack_ms":       10,
		"release_ms":      25,
		"min_open_ms":     90,
		"min_closed_ms":   50,
		"interval_ms":     25,
	}

	def __init__(self, key: str, audio_sync: Optional[dict], label: str = "") -> None:
		self.key = str(key or 'x').lower()
		self.label = label or self.key

		values = dict(self.DEFAULTS)
		if isinstance(audio_sync, dict):
			for name, default in self.DEFAULTS.items():
				if name not in audio_sync:
					continue
				raw = audio_sync[name]
				if not isinstance(raw, (int, float)) or isinstance(raw, bool):
					print(f"VoicePlayer: {self.label}.audio_sync.{name} must be a number, ignoring '{raw}'")
					continue
				values[name] = type(default)(raw)

		self.open_threshold  = values["open_threshold"]
		self.close_threshold = values["close_threshold"]
		self.attack_ms       = max(1, values["attack_ms"])
		self.release_ms      = max(1, values["release_ms"])
		self.min_open_ms     = max(0, values["min_open_ms"])
		self.min_closed_ms   = max(0, values["min_closed_ms"])
		self.interval_ms     = max(1, values["interval_ms"])

		if self.close_threshold >= self.open_threshold:
			print(f"VoicePlayer: {self.label} close_threshold >= open_threshold — hysteresis off, may chatter.")

		self._a_attack  = 1.0 - math.exp(-self.interval_ms / float(self.attack_ms))
		self._a_release = 1.0 - math.exp(-self.interval_ms / float(self.release_ms))

		self.reset()

	def reset(self) -> None:
		self.envelope = 0.0
		self.is_open = False
		self._last_change = -1e9

	def step(self, rms: float, t_ms: float) -> Optional[int]:
		"""Advance one analysis window. Returns 1 or 0 if the state changed,
		otherwise None."""
		coeff = self._a_attack if rms > self.envelope else self._a_release
		self.envelope += coeff * (rms - self.envelope)

		if self.is_open:
			want_open = self.envelope > self.close_threshold
		else:
			want_open = self.envelope > self.open_threshold

		if want_open == self.is_open:
			return None

		hold = self.min_open_ms if self.is_open else self.min_closed_ms
		if (t_ms - self._last_change) < hold:
			return None

		self.is_open = want_open
		self._last_change = t_ms
		return 1 if want_open else 0

	def describe(self) -> str:
		max_hz = 1000.0 / max(1, self.min_open_ms + self.min_closed_ms)
		return (f"'{self.key}' ({self.label}) open={self.open_threshold} close={self.close_threshold} "
		        f"attack={self.attack_ms}ms release={self.release_ms}ms "
		        f"hold={self.min_open_ms}/{self.min_closed_ms}ms (max {max_hz:.1f} Hz)")


class VoicePlayer:
	VOICE_VOLUME = 1.0

	def __init__(
		self,
		pygame_instance: Any,
		voices_dir: str,
		hardware_path: Optional[str] = None,
	) -> None:
		self.pygame = pygame_instance

		# Movements driven by the audio envelope, resolved from the character
		# JSON so nothing is hardcoded to a particular key.
		self.channels: List[_SyncChannel] = []
		self.interval_ms: int = _SyncChannel.DEFAULTS["interval_ms"]

		self.apply_hardware_config(hardware_path)

		self._stop_event = threading.Event()
		self._thread: Optional[threading.Thread] = None

		self._local_voices_dir = voices_dir

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

	def apply_hardware_config(self, hardware_path: Optional[str]) -> None:
		"""Read the character JSON and build a sync channel for every movement
		carrying an "audio_sync" block. Falls back to the movement keyed 'x' so
		character configs without any block keep working.
		"""
		movements = []
		if hardware_path:
			try:
				with open(hardware_path, 'r') as f:
					movements = json.load(f).get('movements', [])
			except (OSError, ValueError) as e:
				print(f"VoicePlayer: could not read character config '{hardware_path}': {e}")

		self.channels = []
		for m in movements:
			if not isinstance(m.get('audio_sync'), dict):
				continue
			self.channels.append(_SyncChannel(
				key=m.get('key') or 'x',
				audio_sync=m['audio_sync'],
				label=m.get('description', ''),
			))

		if not self.channels:
			self.channels.append(_SyncChannel(key='x', audio_sync=None, label='Mouth'))

		# The RMS analysis grid is shared, so one interval governs all channels.
		self.interval_ms = self.channels[0].interval_ms
		for ch in self.channels[1:]:
			if ch.interval_ms != self.interval_ms:
				print(f"VoicePlayer: {ch.label} interval_ms={ch.interval_ms} ignored; "
				      f"using {self.interval_ms} from {self.channels[0].label}.")

		for ch in self.channels:
			print(f"VoicePlayer: driving {ch.describe()}")

	def play(self, filename: str) -> None:
		"""Play a single voice file, stopping anything currently playing."""
		self._stop_current()
		self._stop_event.clear()
		self._thread = threading.Thread(
			target=self._play_sequence_worker,
			args=([filename],),
			daemon=True,
		)
		self._thread.start()

	def play_sequence(self, filenames: List[str]) -> None:
		"""Play a list of voice files in order, stopping anything currently playing."""
		self._stop_current()
		self._stop_event.clear()
		self._thread = threading.Thread(
			target=self._play_sequence_worker,
			args=(list(filenames),),
			daemon=True,
		)
		self._thread.start()

	def stop(self) -> None:
		"""Stop all playback immediately."""
		dispatcher.send(signal="voicePlaybackEvent", bPlaying=False)
		self._stop_current()

	# -------------------------------------------------------------------------
	# Internal
	# -------------------------------------------------------------------------

	def _stop_current(self) -> None:
		"""Signal any active playback to stop and wait for it to finish."""
		self._stop_event.set()
		if self.pygame.mixer.get_init():
			self.pygame.mixer.music.stop()
		if self._thread and self._thread.is_alive():
			self._thread.join(timeout=2)
		self._thread = None

	def _resolve_path(self, filename: str) -> Optional[str]:
		"""Try the filename as-is, then /mnt/usb/voices/, then local voices/."""
		if os.path.isfile(filename):
			return filename

		usb_path = os.path.join(USB_VOICES_DIR, filename)
		if os.path.isfile(usb_path):
			return usb_path

		local_path = os.path.join(self._local_voices_dir, filename)
		if os.path.isfile(local_path):
			return local_path

		print(f"VoicePlayer: file not found: '{filename}' (checked as-is, USB, and local voices/)")
		return None

	def _wake_dac_if_needed(self) -> None:
		"""Wake the DAC if it has been idle too long.

		Delegates to audio_setup so VoicePlayer and ShowPlayer share one idle
		timer — there is only one DAC, and two independent timers meant each
		player fired a redundant wake tone after the other had just played.
		"""
		audio_setup.wake_dac_if_needed(self.pygame)

	def _play_sequence_worker(self, filenames: List[str]) -> None:
		print(f"VoicePlayer: worker started, {len(filenames)} file(s)")
		self._wake_dac_if_needed()
		dispatcher.send(signal="voicePlaybackEvent", bPlaying=True)
		dispatcher.send(signal="updateStatus", id="Voice Playback", value="Speaking...")
		for filename in filenames:
			if self._stop_event.is_set():
				break

			path = self._resolve_path(filename)
			if path is None:
				continue

			try:
				self._play_file(path)
			except Exception as e:
				print(f"VoicePlayer: error playing '{filename}': {e}")

		self._release_channels()
		print(f"VoicePlayer: worker done, dispatching bPlaying=False")
		dispatcher.send(signal="voicePlaybackEvent", bPlaying=False)

	def _release_channels(self) -> None:
		"""Drop every audio-driven movement. Called when a file or sequence
		ends, and on stop, so nothing is left energised — the head channel in
		particular has no max_sec watchdog behind it."""
		for ch in self.channels:
			if ch.is_open:
				ch.is_open = False
			dispatcher.send(signal="keyEvent", key=ch.key, val=0)

	def _play_file(self, file_path: str) -> None:
		if not self.pygame.mixer.get_init():
			print(f"VoicePlayer: mixer not initialized, skipping '{file_path}'")
			return

		sample_rate, data = self._load_audio_data(file_path)
		rms_values = self._calculate_rms(data, sample_rate)

		buf = io.BytesIO()
		wavfile.write(buf, sample_rate, data)
		buf.seek(0)

		self.pygame.mixer.music.load(buf)
		self.pygame.mixer.music.set_volume(self.VOICE_VOLUME)
		self.pygame.mixer.music.play()

		for ch in self.channels:
			ch.reset()

		start_time = time.monotonic()
		for iteration, rms in enumerate(rms_values):
			if self._stop_event.is_set():
				break

			# Every channel sees the same RMS window but keeps its own envelope,
			# thresholds and hold timers, so each moves at its own rate.
			t_ms = iteration * self.interval_ms
			for ch in self.channels:
				val = ch.step(rms, t_ms)
				if val is not None:
					dispatcher.send(signal="keyEvent", key=ch.key, val=val)

			target_time = start_time + (iteration + 1) * (self.interval_ms / 1000.0)
			sleep_duration = target_time - time.monotonic()
			if sleep_duration > 0:
				time.sleep(sleep_duration)

		# Never leave a movement energised between files in a sequence.
		self._release_channels()

		# Wait for playback to finish with a safety timeout
		deadline = time.monotonic() + 30
		while self.pygame.mixer.music.get_busy() and not self._stop_event.is_set():
			if time.monotonic() > deadline:
				print("VoicePlayer: playback timeout, forcing stop.")
				self.pygame.mixer.music.stop()
				break
			time.sleep(0.01)

		audio_setup.note_playback()

	def _load_audio_data(self, file_path: str):
		"""Load audio data and sample rate from mp3, ogg, or wav."""
		if file_path.endswith('.mp3'):
			audio = AudioSegment.from_mp3(file_path)
		elif file_path.endswith('.ogg'):
			audio = AudioSegment.from_ogg(file_path)
		elif file_path.endswith('.wav'):
			audio = AudioSegment.from_wav(file_path)
		else:
			raise ValueError(f"Unsupported file format: {file_path}")

		# Normalize to 16-bit PCM so scipy can handle it regardless of source format
		audio = audio.set_sample_width(2)

		wav_io = io.BytesIO()
		audio.export(wav_io, format="wav")
		wav_io.seek(0)
		sample_rate, data = wavfile.read(wav_io)
		return sample_rate, data

	def _calculate_rms(self, data: np.ndarray, sample_rate: int):
		"""Calculate normalized RMS values over time."""
		window_size = int(sample_rate * (self.interval_ms / 1000.0))
		num_samples = len(data)
		max_rms = float(np.max(np.abs(data))) or 1.0  # avoid divide-by-zero
		rms_values = []

		for i in range(0, num_samples, window_size):
			window = data[i:i + window_size]
			rms = np.sqrt(np.mean(window.astype(np.float64) ** 2))
			if np.isnan(rms) or np.isinf(rms):
				rms = 0.0
			rms_values.append(min(rms / max_rms, 1.0))

		return rms_values