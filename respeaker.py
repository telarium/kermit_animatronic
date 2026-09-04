#!/usr/bin/env python3
"""
respeaker.py — the one owner of the ReSpeaker XVF3800's USB control interface.

The DSP settings and the LED ring are both vendor control transfers to the same
endpoint on the same chip, so they share one handle and one lock here. They used
to be split: usb_monitor shelled out to xvf_host.py as a CLI (two interpreter
startups per setting) while led_controller imported the same file in-process,
and nothing coordinated them — a reboot that resets every parameter to default
could land while the LED worker held a handle mid-write.

Capture is a separate concern and lives in mic_stream.py. The two meet at one
place only: the beam configuration written here decides what the two USB capture
channels carry, and mic_stream.BEAM_CHANNEL decides which one is read. Change
one and check the other.
"""

import importlib.util
import os
import threading
import time
from typing import Optional

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- DSP settings -----------------------------------------------------------
# Applied after every firmware reboot, because REBOOT resets ALL parameters to
# their defaults (see the XVF3800 control table) and initialize() reboots the
# device at every startup. Without this the device comes up with the stock
# configuration, which is unusable here for two reasons:
#
#   AUDIO_MGR_MIC_GAIN defaults to 90 (a linear factor, ~39dB) applied before
#   any processing. At that level ordinary speech from a few feet away pins the
#   converter at full scale, and the AGC then ramps its gain down hard in
#   response — the signal arrives clipped, then collapses.
#
#   The beamformer defaults to a free-running auto-select beam. Measured with
#   AEC_AZIMUTH_VALUES while a stationary talker spoke continuously, it settled
#   on the talker only about a third of the time and otherwise snapped to
#   cardinal angles (pi/2, pi, 3pi/2). While it was pointed away, speech was
#   nulled out completely — whole utterances arriving at the noise floor. That
#   is what made follow-up answers intermittently unheard.
#
# So: fix the input gain, stop the AGC adapting, and pin the beams to where
# people actually stand rather than letting the selector hunt.
#
# AEC_FIXEDBEAMSGATING is deliberately NOT set here. It silences beams it
# judges inactive, which is the same class of behaviour as the fault above.
# It defaults to 0; leave it there.
#
# The azimuths are in radians and are specific to how the ReSpeaker sits in
# THIS puppet. To re-measure after moving the board or the mic, run:
#     while true; do sudo python3 xvf_host.py AEC_AZIMUTH_VALUES; sleep 0.5; done
# and talk from the audience position, facing the mic. The third value is the
# free-running beam. Discard readings that land exactly on 1.571 / 3.142 /
# 4.712 / 6.283 — those are the estimator giving up, not a direction — and
# straddle the remaining cluster.
DSP_SETTINGS = [
	# Set explicitly rather than relied on as a default: with ASROUT off the
	# USB channels carry per-microphone AEC residuals, not beams, and
	# mic_stream's channel choice would be reading a bare mic instead.
	("AEC_ASROUTONOFF",              [1]),
	("AUDIO_MGR_MIC_GAIN",           [200.0]),
	("PP_AGCONOFF",                  [0]),
	("AEC_FIXEDBEAMSONOFF",          [1]),
	("AEC_FIXEDBEAMSAZIMUTH_VALUES", [3.60, 4.00]),
]

# The control interface answers a bus scan before it will accept commands.
SETTLE_SECONDS = 2.0
# Reboot drops the device off the bus and brings it back as a fresh enumeration.
REBOOT_GONE_TIMEOUT = 10.0
REBOOT_BACK_TIMEOUT = 30.0


def _import_xvf_host():
	"""xvf_host.py lives at the repo root; setup.py also clones a copy under
	lib/respeaker. Resolved once, here, so every caller uses the same one."""
	cloned = os.path.join(_BASE_DIR, "lib", "respeaker", "python_control", "xvf_host.py")
	if os.path.isfile(cloned):
		spec = importlib.util.spec_from_file_location("xvf_host", cloned)
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		return module
	try:
		import xvf_host
		return xvf_host
	except ImportError:
		return None


class ReSpeaker:
	"""Serialized access to the XVF3800 control endpoint.

	Every method takes the lock, reconnects if the handle went stale, and
	returns a bool/None rather than raising — the device disappears on every
	reboot and unplug, and no caller on the audio path should have to care.
	"""

	def __init__(self) -> None:
		self._xvf = _import_xvf_host()
		self._lock = threading.RLock()
		self._device = None
		self._warned_missing = False
		if self._xvf is None:
			print("ReSpeaker: xvf_host not importable — device control disabled.")

	@property
	def available(self) -> bool:
		return self._xvf is not None

	# -------------------------------------------------------------------------
	# Connection
	# -------------------------------------------------------------------------

	def _connect(self) -> bool:
		"""Open the device if it isn't already open. Caller holds the lock."""
		if self._device is not None:
			return True
		if self._xvf is None:
			return False
		try:
			self._device = self._xvf.find()
		except Exception:
			self._device = None

		if self._device is None:
			if not self._warned_missing:
				print("ReSpeaker: device not found, will retry.")
				self._warned_missing = True
			return False

		self._warned_missing = False
		return True

	def _disconnect(self) -> None:
		if self._device is not None:
			try:
				self._device.close()
			except Exception:
				pass
			self._device = None

	def is_present(self) -> bool:
		"""True if the device currently answers on the bus."""
		if self._xvf is None:
			return False
		try:
			found = self._xvf.find()
		except Exception:
			return False
		if found is None:
			return False
		try:
			found.close()
		except Exception:
			pass
		return True

	# -------------------------------------------------------------------------
	# Parameter access
	# -------------------------------------------------------------------------

	def write(self, name: str, values: list) -> bool:
		"""Write one parameter. Returns False on any failure.

		xvf_host.write() returns silently on an unknown parameter name, so it
		is checked here — otherwise a typo reads as success.
		"""
		if self._xvf is None:
			return False
		if name not in self._xvf.PARAMETERS:
			print(f"ReSpeaker: unknown parameter '{name}', ignoring.")
			return False

		with self._lock:
			if not self._connect():
				return False
			try:
				self._device.write(name, values)
				return True
			except Exception as e:
				print(f"ReSpeaker: write {name} failed ({e}), will reconnect.")
				self._disconnect()
				return False

	def read(self, name: str) -> Optional[tuple]:
		"""Read one parameter. Returns None on any failure."""
		if self._xvf is None:
			return None
		if name not in self._xvf.PARAMETERS:
			print(f"ReSpeaker: unknown parameter '{name}', ignoring.")
			return None

		with self._lock:
			if not self._connect():
				return None
			try:
				return self._device.read(name)
			except Exception as e:
				print(f"ReSpeaker: read {name} failed ({e}), will reconnect.")
				self._disconnect()
				return None

	# -------------------------------------------------------------------------
	# Lifecycle
	# -------------------------------------------------------------------------

	def _wait_for(self, present: bool, timeout: float) -> bool:
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			if self.is_present() == present:
				return True
			time.sleep(0.25)
		return False

	def reboot(self) -> bool:
		"""Reboot the firmware into a known state.

		Waits for the device to actually LEAVE the bus before waiting for it to
		come back. Polling only for presence returns immediately — the device
		is still enumerated for a moment after the command — and the settings
		that follow then land on a device that is about to reset them.
		"""
		if self._xvf is None:
			return False

		with self._lock:
			if not self._connect():
				return False
			try:
				# The device drops off mid-transfer, so a failure here is
				# expected and does not mean the reboot didn't take.
				self._device.write("REBOOT", [1])
			except Exception:
				pass
			self._disconnect()

		print("ReSpeaker: rebooting...")
		if not self._wait_for(present=False, timeout=REBOOT_GONE_TIMEOUT):
			print("ReSpeaker: device never left the bus after REBOOT.")
		if not self._wait_for(present=True, timeout=REBOOT_BACK_TIMEOUT):
			print("ReSpeaker: device did not re-enumerate after reboot.")
			return False

		time.sleep(SETTLE_SECONDS)
		print("ReSpeaker: reboot complete.")
		return True

	def apply_dsp_settings(self) -> None:
		"""Write the DSP settings and read each one back.

		Read-back matters: several controls are mode-dependent and are silently
		ignored if the device is in the wrong state. Logging the value the
		device actually holds means a setting that didn't take shows up here at
		startup, rather than as unexplained deafness weeks later.
		"""
		for name, values in DSP_SETTINGS:
			if not self.write(name, values):
				continue
			actual = self.read(name)
			print(f"ReSpeaker: {name} = {actual} (set {values})")

	def initialize(self) -> None:
		"""Startup bring-up: reboot to a clean state, then apply the settings
		the reboot just wiped."""
		if self._xvf is None:
			return
		if not self.reboot():
			# Still worth trying — the device may be up and simply not have
			# accepted the reboot.
			print("ReSpeaker: applying settings without a confirmed reboot.")
		self.apply_dsp_settings()

	def shutdown(self) -> None:
		with self._lock:
			self._disconnect()


# One device, one instance.
_device_lock = threading.Lock()
_instance: Optional[ReSpeaker] = None


def get_device() -> ReSpeaker:
	global _instance
	with _device_lock:
		if _instance is None:
			_instance = ReSpeaker()
		return _instance


def initialize() -> None:
	get_device().initialize()


if __name__ == "__main__":
	initialize()
