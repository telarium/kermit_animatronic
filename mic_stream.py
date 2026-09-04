#!/usr/bin/env python3
"""
mic_stream.py — shared microphone capture from the ReSpeaker XVF3800.

One arecord process, opened once and never closed, feeding every consumer.

Previously the wakeword and STT each spawned their own arecord and handed the
device back and forth. That handoff cost real time — the wakeword's blocking
stdout.read() had to return, then terminate + wait, then STT spawned a new
process and waited for ALSA to open — and NOTHING was recording for the
duration. Anything said in that window was gone, which is why the first word
went missing intermittently.

With a single always-open stream there is no gap, and the ring buffer below
means STT can be handed the audio from BEFORE it started listening.

This is the capture plane only. The device's control interface — DSP settings,
beam configuration, LEDs — belongs to respeaker.py. BEAM_CHANNEL below is the
one place the two planes have to agree.
"""

import collections
import queue
import re
import subprocess
import threading
import time
from typing import Optional

MIC_RATE = 16000
# 1280 frames = 80ms at 16kHz. This is openwakeword's required chunk size, and
# sherpa-onnx accepts any size, so a single granularity serves both consumers.
MIC_CHUNK_FRAMES = 1280
MIC_PREROLL_SECONDS = 4.0

# The XVF3800 presents a stereo capture endpoint. With AEC_ASROUTONOFF=1 and
# fixed beam mode on (see respeaker.DSP_SETTINGS) the two channels carry fixed
# beam 1 and fixed beam 2 — not left/right microphones.
#
# Capture at the device's real channel count and pick the beam explicitly.
# Asking arecord for one channel does NOT give the first channel: plughw's
# channel conversion mixes the two down, summing two beams steered at different
# angles at the same talker, which combs rather than reinforces.
DEVICE_CHANNELS = 2
BEAM_CHANNEL = 0

# Per-subscriber backlog. A consumer that stalls drops its oldest audio rather
# than growing without bound or blocking the reader for everyone else.
_MIC_QUEUE_CHUNKS = 64

_mic_lock = threading.Lock()
_mic = None


def find_capture_device() -> str:
	"""Find the ReSpeaker's ALSA capture device string (e.g. plughw:1,0).

	Reads `arecord -l` directly — ALSA always lists the device correctly, even
	when PortAudio's cache has gone stale.
	"""
	for attempt in range(20):
		try:
			out = subprocess.run(
				["arecord", "-l"], capture_output=True, text=True
			).stdout
			for line in out.splitlines():
				if "respeaker" in line.lower() and line.strip().lower().startswith("card"):
					m = re.search(r"card (\d+):.*device (\d+):", line)
					if m:
						return f"plughw:{m.group(1)},{m.group(2)}"
		except Exception as e:
			print(f"Mic: error scanning arecord -l: {e}")
		print(f"Mic: ReSpeaker not found, retrying ({attempt + 1}/20)...")
		time.sleep(1)
	raise RuntimeError("ReSpeaker not found — is it plugged in?")


class _MicStream:
	def __init__(self) -> None:
		self._stop = threading.Event()
		self._thread: Optional[threading.Thread] = None
		self._subs: list = []
		self._subs_lock = threading.Lock()
		self._ring = collections.deque(
			maxlen=int(MIC_PREROLL_SECONDS * MIC_RATE / MIC_CHUNK_FRAMES)
		)
		self._ring_lock = threading.Lock()
		self._anchor: float = 0.0
		# Gate for the animatronic's own voice. The stream stays open (arecord must keep
		# being drained or its pipe fills and it dies) but captured audio is
		# discarded rather than buffered or delivered.
		self._muted = False

	# -- lifecycle --

	def start(self) -> None:
		if self._thread and self._thread.is_alive():
			return
		self._stop.clear()
		self._thread = threading.Thread(target=self._reader, daemon=True)
		self._thread.start()

	def stop(self) -> None:
		self._stop.set()
		if self._thread and self._thread.is_alive():
			self._thread.join(timeout=3)
		self._thread = None

	# -- mute --

	def set_muted(self, muted: bool) -> None:
		if muted == self._muted:
			return
		self._muted = muted
		if muted:
			return

		# Unmuting: throw away everything captured up to this instant. The
		# tail end of the animatronic's own audio is still in flight through ALSA's
		# buffer, and the ring may hold pre-mute audio that is now stale.
		with self._ring_lock:
			self._ring.clear()
		with self._subs_lock:
			subs = list(self._subs)
		for q in subs:
			while True:
				try:
					q.get_nowait()
				except queue.Empty:
					break

	# -- consumers --

	def subscribe(self):
		q = queue.Queue(maxsize=_MIC_QUEUE_CHUNKS)
		with self._subs_lock:
			self._subs.append(q)
		return q

	def unsubscribe(self, q) -> None:
		with self._subs_lock:
			if q in self._subs:
				self._subs.remove(q)

	# -- preroll --

	def set_anchor(self) -> None:
		"""Mark 'now' as the point a consumer will later want audio from."""
		self._anchor = time.monotonic()

	def audio_since_anchor(self, max_seconds: float = 2.0, lead_seconds: float = 0.0):
		"""Return mono int16 audio captured since the last set_anchor().

		lead_seconds starts the window slightly BEFORE the anchor. openwakeword
		confirms a phrase a few hundred ms after it ends, so with a lead of
		zero anything said in that gap sits in the ring but falls outside the
		window and is discarded — which is exactly the audio a fast speaker
		runs the command into.

		Capped at max_seconds so a slow handler, or an anchor nobody reset,
		can't drag a long stretch of history into the recognizer.
		"""
		import numpy as np

		if self._anchor == 0.0:
			return np.zeros(0, dtype=np.int16)
		start = self._anchor - max(lead_seconds, 0.0)
		with self._ring_lock:
			chunks = [c for ts, c in self._ring if ts >= start]
		if not chunks:
			return np.zeros(0, dtype=np.int16)
		audio = np.concatenate(chunks)
		limit = int(max_seconds * MIC_RATE)
		if audio.size > limit:
			audio = audio[-limit:]
		return audio

	# -- reader --

	def _reader(self) -> None:
		import numpy as np

		chunk_bytes = MIC_CHUNK_FRAMES * 2 * DEVICE_CHANNELS
		while not self._stop.is_set():
			proc = None
			try:
				device = find_capture_device()
				proc = subprocess.Popen(
					[
						"arecord",
						"-D", device,
						"-f", "S16_LE",
						"-r", str(MIC_RATE),
						"-c", str(DEVICE_CHANNELS),
						"-t", "raw",
					],
					stdout=subprocess.PIPE,
					stderr=subprocess.DEVNULL,
				)
				print(f"Mic: shared capture started on {device} "
				      f"({DEVICE_CHANNELS}ch, reading beam {BEAM_CHANNEL}, pid={proc.pid})")

				while not self._stop.is_set():
					raw = proc.stdout.read(chunk_bytes)
					if not raw or len(raw) < chunk_bytes:
						break  # arecord died — fall through and reopen

					if self._muted:
						continue

					frames = np.frombuffer(raw, dtype=np.int16).reshape(-1, DEVICE_CHANNELS)
					# frombuffer is read-only, so copy before handing it out.
					mono = frames[:, BEAM_CHANNEL].copy()

					with self._ring_lock:
						self._ring.append((time.monotonic(), mono))

					with self._subs_lock:
						subs = list(self._subs)
					for q in subs:
						try:
							q.put_nowait(mono)
						except queue.Full:
							# Drop this subscriber's oldest chunk to make room.
							# Never block: one slow consumer must not stall
							# capture for the other.
							try:
								q.get_nowait()
								q.put_nowait(mono)
							except (queue.Empty, queue.Full):
								pass

			except Exception as e:
				print(f"Mic: capture error: {e}")
			finally:
				if proc is not None:
					try:
						proc.terminate()
						proc.wait(timeout=2)
					except Exception:
						try:
							proc.kill()
						except Exception:
							pass

			if not self._stop.is_set():
				# Covers the ReSpeaker being unplugged and replugged: rediscover
				# the device on the next pass rather than dying permanently.
				print("Mic: capture dropped, reopening...")
				time.sleep(0.5)

		print("Mic: shared capture stopped.")


def _get_mic() -> _MicStream:
	global _mic
	with _mic_lock:
		if _mic is None:
			_mic = _MicStream()
		_mic.start()
		return _mic


def subscribe():
	"""Subscribe to the shared mic. Returns a Queue of mono int16 chunks."""
	return _get_mic().subscribe()


def unsubscribe(q) -> None:
	if _mic is not None:
		_mic.unsubscribe(q)


def set_anchor() -> None:
	"""Mark the current moment — used by the wakeword to timestamp detection."""
	_get_mic().set_anchor()


def audio_since_anchor(max_seconds: float = 2.0, lead_seconds: float = 0.0):
	"""Audio captured since set_anchor(), as mono int16."""
	return _get_mic().audio_since_anchor(max_seconds, lead_seconds)


def set_muted(muted: bool) -> None:
	"""Gate the shared mic while the animatronic is speaking.

	Must be paired: every set_muted(True) needs a matching False, or the
	microphone stays deaf. Unmuting flushes all buffered audio.
	"""
	_get_mic().set_muted(muted)


def stop() -> None:
	"""Close the shared capture stream. Called on shutdown."""
	global _mic
	with _mic_lock:
		if _mic is not None:
			_mic.stop()
			_mic = None
