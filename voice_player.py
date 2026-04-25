from pydub import AudioSegment
from pydispatch import dispatcher
from scipy.io import wavfile
import pygame
import numpy as np
import time
import io
import os
import threading
from typing import List, Optional, Any

USB_VOICES_DIR = "/mnt/usb/voices"

class VoicePlayer:
	def __init__(self, pygame_instance: Any, threshold: float = 0.15, interval_ms: int = 25) -> None:
		self.pygame = pygame_instance

		if not isinstance(threshold, (int, float)):
			raise ValueError("Threshold must be a numeric value!")

		self.threshold: float = threshold
		self.interval_ms: int = interval_ms

		self._stop_event = threading.Event()
		self._thread: Optional[threading.Thread] = None

		script_dir = os.path.dirname(os.path.abspath(__file__))
		self._local_voices_dir = os.path.join(script_dir, "voices")

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

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

	def _play_sequence_worker(self, filenames: List[str]) -> None:
		dispatcher.send(signal="voicePlaybackEvent", bPlaying=True)
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

		# Ensure mouth is closed when done
		dispatcher.send(signal="keyEvent", key='x', val=0)
		dispatcher.send(signal="voicePlaybackEvent", bPlaying=False)

	def _play_file(self, file_path: str) -> None:
		print(f"VoicePlayer: loading {file_path}")
		sample_rate, data = self._load_audio_data(file_path)
		print(f"VoicePlayer: loaded {len(data)/sample_rate:.2f}s at {sample_rate}Hz")
		rms_values = self._calculate_rms(data, sample_rate)

		buf = io.BytesIO()
		wavfile.write(buf, sample_rate, data)
		buf.seek(0)

		self.pygame.mixer.music.load(buf)
		self.pygame.mixer.music.play()
		print(f"VoicePlayer: playback started")

		start_time = time.monotonic()
		...

		# Wait for playback to finish (unless stopped)
		print(f"VoicePlayer: RMS loop done, waiting for mixer...")
		while self.pygame.mixer.music.get_busy() and not self._stop_event.is_set():
			self.pygame.time.wait(10)
		print(f"VoicePlayer: done")
		
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
