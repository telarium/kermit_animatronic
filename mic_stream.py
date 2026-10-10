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
import os
import queue
import re
import subprocess
import threading
import time
from typing import Optional
from logger import get_logger

log = get_logger(__name__)


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

# Reopen backoff. When the ReSpeaker gets into a bad state it can accept an
# open and then fail the stream within milliseconds, over and over. Reopening
# every 0.5s turned one bad minute into 40+ open/cancel cycles, and every
# cancel of in-flight USB audio transfers runs through the xHCI code path that
# has crashed the kernel (xhci_invalidate_cancelled_tds) on 5.15.199. Back off
# exponentially instead: 1, 2, 4, 8, 16, 30, 30... seconds between attempts.
_REOPEN_DELAY_MIN = 1.0
_REOPEN_DELAY_MAX = 30.0
# A stream that stays up this long counts as healthy and resets the backoff.
_STABLE_SECONDS = 10.0

# Mic-loss watchdog. Reboots only when BOTH are true:
#   1. no audio has arrived for _WATCHDOG_TIMEOUT, and
#   2. the kernel's USB hub thread (a kworker running usb_hub_wq) has been
#      stuck in uninterruptible sleep (state D) for _HUB_STUCK_TIMEOUT.
# Condition 2 is the signature of the 5.15.199 hang: a ReSpeaker disconnect
# leaves the hub thread blocked in usb_kill_urb forever, so no USB device can
# come or go until a reboot. Someone simply unplugging the ReSpeaker (or never
# fitting one) leaves the hub thread idle, so the mic is optional and an
# unplug never causes a reboot.
_WATCHDOG_ENABLED = True
_WATCHDOG_TIMEOUT = 180.0
_WATCHDOG_POLL = 10.0
# If the graceful reboot hasn't happened after this long (stuck USB processes
# can block shutdown), force it.
_WATCHDOG_FORCE_AFTER = 90.0
# The hub thread is briefly in state D during every normal connect/disconnect;
# a real hang keeps it there indefinitely.
_HUB_STUCK_TIMEOUT = 60.0
# Reboot-loop guard. Counts consecutive watchdog reboots in a small file; after
# this many in a row without the mic coming back, stop rebooting and stay up
# (deaf, but reachable). The count resets once the mic is healthy for a while.
_WATCHDOG_MAX_REBOOTS = 3
_WATCHDOG_RESET_AFTER = 600.0
_WATCHDOG_STATE_FILE = os.path.join(
	os.path.dirname(os.path.abspath(__file__)), "logs", ".mic_watchdog_reboots"
)

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
			log.exception(f"Mic: error scanning arecord -l: {e}")
		log.warning(f"Mic: ReSpeaker not found, retrying ({attempt + 1}/20)...")
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
		# Last time a full chunk was read from arecord (muted or not). Drives
		# the mic-loss watchdog.
		self._last_audio: float = time.monotonic()
		self._healthy_since: Optional[float] = None
		# When the USB hub thread was first seen stuck (None = not stuck).
		self._hub_stuck_since: Optional[float] = None
		self._watchdog: Optional[threading.Thread] = None
		# Gate for the animatronic's own voice. The stream stays open (arecord must keep
		# being drained or its pipe fills and it dies) but captured audio is
		# discarded rather than buffered or delivered.
		self._muted = False

	# -- lifecycle --

	def start(self) -> None:
		if self._thread and self._thread.is_alive():
			return
		self._stop.clear()
		self._last_audio = time.monotonic()
		self._thread = threading.Thread(target=self._reader, daemon=True)
		self._thread.start()
		if _WATCHDOG_ENABLED and not (self._watchdog and self._watchdog.is_alive()):
			self._watchdog = threading.Thread(target=self._watchdog_loop, daemon=True)
			self._watchdog.start()

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
		# Consecutive drops without a healthy stream in between. Drives the
		# reopen backoff and is reset once a stream survives _STABLE_SECONDS.
		failures = 0
		while not self._stop.is_set():
			proc = None
			opened_at: Optional[float] = None
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
				opened_at = time.monotonic()
				log.info(f"Mic: shared capture started on {device} "
					f"({DEVICE_CHANNELS}ch, reading beam {BEAM_CHANNEL}, pid={proc.pid})")

				while not self._stop.is_set():
					raw = proc.stdout.read(chunk_bytes)
					if not raw or len(raw) < chunk_bytes:
						break  # arecord died — fall through and reopen

					# Audio is flowing. Recorded before the mute check: muted
					# audio still proves the device and USB path are alive.
					self._last_audio = time.monotonic()

					if failures and time.monotonic() - opened_at >= _STABLE_SECONDS:
						log.info(f"Mic: capture stable again after {failures} failed attempt(s).")
						failures = 0

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
				log.exception(f"Mic: capture error: {e}")
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

			if self._stop.is_set():
				break

			# Covers the ReSpeaker being unplugged and replugged: rediscover the
			# device on the next pass rather than dying permanently — but back off
			# so a device stuck in a bad state isn't hammered with reopens.
			ran_for = time.monotonic() - opened_at if opened_at is not None else 0.0
			if ran_for >= _STABLE_SECONDS:
				failures = 0
			failures += 1
			delay = min(_REOPEN_DELAY_MIN * (2 ** (failures - 1)), _REOPEN_DELAY_MAX)
			log.warning(f"Mic: capture dropped after {ran_for:.1f}s, "
				f"reopening in {delay:.0f}s (attempt {failures})...")
			# Event.wait instead of sleep so shutdown isn't held up by a long backoff.
			self._stop.wait(delay)

		log.info("Mic: shared capture stopped.")

	# -- watchdog --

	def _watchdog_loop(self) -> None:
		"""Reboot the system if the mic has been silent for _WATCHDOG_TIMEOUT.

		Runs in its own thread so it still fires when the reader thread is
		blocked inside a stuck arecord read.
		"""
		while not self._stop.wait(_WATCHDOG_POLL):
			now = time.monotonic()
			silent_for = now - self._last_audio

			if silent_for < _WATCHDOG_POLL * 2:
				# Healthy. After a sustained healthy stretch, clear the
				# reboot-loop counter so a future hang gets its full budget.
				if self._healthy_since is None:
					self._healthy_since = now
				elif now - self._healthy_since >= _WATCHDOG_RESET_AFTER:
					if _read_reboot_count():
						_write_reboot_count(0)
						log.info("Mic watchdog: mic healthy, reboot counter cleared.")
					self._healthy_since = now
				continue

			self._healthy_since = None

			# Track how long the USB hub thread has been stuck. Checked even
			# before the timeout so the stuck time is measured accurately.
			if _usb_hub_stuck():
				if self._hub_stuck_since is None:
					self._hub_stuck_since = now
			else:
				self._hub_stuck_since = None

			if silent_for < _WATCHDOG_TIMEOUT:
				continue
			if self._hub_stuck_since is None or now - self._hub_stuck_since < _HUB_STUCK_TIMEOUT:
				# Mic is gone but USB is healthy: it was unplugged or never
				# fitted. Not our problem — keep running without it.
				continue

			count = _read_reboot_count()
			if count >= _WATCHDOG_MAX_REBOOTS:
				log.error(f"Mic watchdog: no audio for {silent_for:.0f}s, but already "
					f"rebooted {count} times in a row — not rebooting again. "
					f"Check the ReSpeaker connection.")
				# Re-arm quietly so this doesn't log every poll.
				self._last_audio = now
				continue

			_write_reboot_count(count + 1)
			log.critical(f"Mic watchdog: no audio for {silent_for:.0f}s and the USB hub "
				f"has been stuck for {now - self._hub_stuck_since:.0f}s — USB is "
				f"wedged. Rebooting (watchdog reboot {count + 1}/"
				f"{_WATCHDOG_MAX_REBOOTS}).")
			_reboot_system()
			return


def _usb_hub_stuck() -> bool:
	"""True if a kworker running the USB hub workqueue is in state D.

	The kernel shows the workqueue in a worker's name, e.g.
	'kworker/0:3+usb_hub_wq'. State D (uninterruptible sleep) for a long
	stretch means it is blocked — the USB hang this watchdog exists for.
	"""
	try:
		pids = [p for p in os.listdir("/proc") if p.isdigit()]
	except OSError:
		return False
	for pid in pids:
		try:
			with open(f"/proc/{pid}/stat") as f:
				stat = f.read()
		except OSError:
			continue
		# Format: pid (comm) state ... — comm may contain spaces/parens.
		lpar, rpar = stat.find("("), stat.rfind(")")
		if lpar < 0 or rpar < 0:
			continue
		comm = stat[lpar + 1:rpar]
		if "usb_hub_wq" in comm and stat[rpar + 2:rpar + 3] == "D":
			return True
	return False


def _read_reboot_count() -> int:
	try:
		with open(_WATCHDOG_STATE_FILE) as f:
			return int(f.read().strip() or 0)
	except (OSError, ValueError):
		return 0


def _write_reboot_count(count: int) -> None:
	try:
		with open(_WATCHDOG_STATE_FILE, "w") as f:
			f.write(f"{count}\n")
			f.flush()
			os.fsync(f.fileno())
	except OSError as e:
		log.warning(f"Mic watchdog: could not write {_WATCHDOG_STATE_FILE}: {e}")


def _run_reboot(args: list) -> bool:
	"""Run a reboot command as-is, then via passwordless sudo if that fails."""
	for cmd in (args, ["sudo", "-n"] + args):
		try:
			if subprocess.run(cmd, timeout=15).returncode == 0:
				return True
		except Exception as e:
			log.warning(f"Mic watchdog: {' '.join(cmd)} failed: {e}")
	return False


def _reboot_system() -> None:
	"""Graceful reboot, then a forced one if stuck USB processes block shutdown."""
	os.sync()
	if _run_reboot(["systemctl", "reboot"]):
		time.sleep(_WATCHDOG_FORCE_AFTER)
		log.critical("Mic watchdog: graceful reboot stalled — forcing.")
	os.sync()
	# -ff: reboot immediately without waiting on services, which is exactly
	# what hangs when processes are stuck on the wedged USB hub.
	if not _run_reboot(["systemctl", "reboot", "-ff"]):
		log.error("Mic watchdog: could not reboot (no permission?). Staying up.")


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
