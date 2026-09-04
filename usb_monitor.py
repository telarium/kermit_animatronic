import os
import time
import threading
import subprocess
import glob
import time
import pyudev
from pydispatch import dispatcher
from typing import Optional

USB_MOUNT_POINT = "/mnt/usb"

# --- ReSpeaker DSP settings -------------------------------------------------
# Applied after every firmware reboot, because REBOOT resets ALL parameters to
# their defaults (see the XVF3800 control table) and init_respeaker() reboots
# the device at every startup. Without this the device comes up with the stock
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
RESPEAKER_SETTINGS = [
	("AUDIO_MGR_MIC_GAIN",           ["200"]),
	("PP_AGCONOFF",                  ["0"]),
	("AEC_FIXEDBEAMSONOFF",          ["1"]),
	("AEC_FIXEDBEAMSAZIMUTH_VALUES", ["3.60", "4.00"]),
]

# The control interface answers lsusb before it is ready to accept commands.
RESPEAKER_SETTLE_SECONDS = 2.0
RESPEAKER_CMD_TIMEOUT = 15


def find_usb_audio_card() -> Optional[str]:
	"""Find the USB audio output card number from aplay -l and return plughw:X,0."""
	try:
		result = subprocess.run(["aplay", "-l"], capture_output=True, text=True)
		for line in result.stdout.splitlines():
			if "usb audio" in line.lower() and "respeaker" not in line.lower():
				card_num = line.split(":")[0].replace("card", "").strip()
				print(f"Audio: found USB audio at card {card_num}")
				return f"plughw:{card_num},0"
	except Exception as e:
		print(f"Audio: error finding USB audio card: {e}")
	print("Audio: USB audio not found.")
	return None


def _xvf_host_path() -> Optional[str]:
	"""Locate xvf_host.py. setup.py clones it under lib/respeaker, but a copy
	also lives at the repo root; prefer the cloned one to match led_controller."""
	script_dir = os.path.dirname(os.path.abspath(__file__))
	candidates = (
		os.path.join(script_dir, "lib", "respeaker", "python_control", "xvf_host.py"),
		os.path.join(script_dir, "xvf_host.py"),
	)
	for path in candidates:
		if os.path.isfile(path):
			return path
	print("ReSpeaker: xvf_host.py not found — run setup.py.")
	return None


def _xvf_run(xvf_py: str, command: str, values: Optional[list] = None) -> Optional[str]:
	"""Run one xvf_host command. Returns its stdout, or None on failure.

	xvf_host exits non-zero and prints to stdout on error, so the return code
	is the thing to check — a write that the firmware ignores still exits 0,
	which is why every setting below is read back rather than assumed.
	"""
	argv = ["python3", xvf_py, command]
	if values:
		argv += ["--values"] + list(values)
	try:
		result = subprocess.run(
			argv, capture_output=True, text=True, timeout=RESPEAKER_CMD_TIMEOUT
		)
	except subprocess.TimeoutExpired:
		print(f"ReSpeaker: '{command}' timed out.")
		return None
	except Exception as e:
		print(f"ReSpeaker: '{command}' failed to run: {e}")
		return None

	if result.returncode != 0:
		detail = (result.stdout or result.stderr or "").strip().splitlines()
		print(f"ReSpeaker: '{command}' failed: {detail[-1] if detail else 'unknown error'}")
		return None
	return result.stdout


def _read_value(output: Optional[str], command: str) -> str:
	"""Pull the '[...]' payload out of xvf_host's read output for logging."""
	if not output:
		return "?"
	for line in output.splitlines():
		if line.startswith(command + ":"):
			return line.split(":", 1)[1].strip()
	return "?"


def apply_respeaker_settings() -> None:
	"""Write the DSP settings and read each one back.

	Read-back matters: several controls are mode-dependent and are silently
	ignored if the device is in the wrong state. Logging the value the device
	actually holds means a setting that didn't take shows up here at startup,
	rather than as unexplained deafness weeks later.
	"""
	xvf_py = _xvf_host_path()
	if xvf_py is None:
		return

	# lsusb sees the device before its control endpoint will answer.
	time.sleep(RESPEAKER_SETTLE_SECONDS)

	for command, values in RESPEAKER_SETTINGS:
		if _xvf_run(xvf_py, command, values) is None:
			continue
		actual = _read_value(_xvf_run(xvf_py, command), command)
		print(f"ReSpeaker: {command} = {actual} (set {' '.join(values)})")


def init_respeaker() -> None:
	"""Reboot ReSpeaker firmware to ensure a clean state at startup, then
	re-apply the DSP settings the reboot just wiped."""
	try:
		xvf_py = _xvf_host_path()
		if xvf_py is None:
			return
		# timeout is essential: if something else holds the ReSpeaker's USB
		# control interface (e.g. a stray arecord from a previous run), this
		# call blocks forever and startup never gets past this line. Failing
		# here is recoverable; hanging is not.
		subprocess.run(
			["python3", xvf_py, "REBOOT", "--values", "1"],
			check=True, timeout=30,
		)
		print("ReSpeaker: rebooting...")

		# Wait for device to re-enumerate
		for attempt in range(20):
			result = subprocess.run(["lsusb"], capture_output=True, text=True)
			if any("respeaker" in line.lower() for line in result.stdout.splitlines()):
				print("ReSpeaker: reboot complete.")
				apply_respeaker_settings()
				return
			time.sleep(0.5)

		print("ReSpeaker: device did not re-enumerate after reboot.")
	except Exception as e:
		print(f"ReSpeaker: init failed: {e}")


def get_mount_point() -> Optional[str]:
	"""Return the mount point if the USB drive is mounted, or None."""
	try:
		result = subprocess.run(
			["findmnt", "-n", "-o", "TARGET", USB_MOUNT_POINT],
			capture_output=True, text=True, timeout=5
		)
		if result.returncode == 0 and result.stdout.strip():
			return result.stdout.strip()
	except Exception as e:
		print(f"USBMonitor: error checking mount point: {e}")
	return None


def is_mounted() -> bool:
	"""Return True if a USB drive is currently mounted."""
	return get_mount_point() is not None


def _is_usb_partition(device: pyudev.Device) -> bool:
	"""Return True if this device is a USB storage partition."""
	if device.device_type != 'partition':
		return False
	return device.find_parent('usb') is not None


def _watch() -> None:
	for device in iter(_monitor.poll, None):
		if not _is_usb_partition(device):
			continue

		device_name = device.sys_name

		if device.action == 'add':
			print(f"USBMonitor: drive connected ({device_name})")
			dispatcher.send(signal="playVoiceFile", file="usb_connected.ogg")
			threading.Timer(2.0, _check_mounted).start()

		elif device.action == 'remove':
			print(f"USBMonitor: drive removed ({device_name})")


def _check_mounted() -> None:
	"""Check if the drive mounted successfully and retry if not."""
	if is_mounted():
		print(f"USBMonitor: drive mounted at {USB_MOUNT_POINT}")
		_look_for_config()
	else:
		print("USBMonitor: drive not yet mounted, retrying...")
		threading.Timer(2.0, _check_mounted).start()


def _look_for_config() -> None:
	"""Look for a .cfg file on the drive and dispatch its path if found."""
	matches = glob.glob(f"{USB_MOUNT_POINT}/*.cfg")
	if matches:
		cfg_path = matches[0]
		print(f"USBMonitor: config file found: {cfg_path}")
		try:
			dispatcher.send(signal='usbConfigFound', path=cfg_path)
		except Exception as e:
			print(f"USBMonitor: error dispatching config file: {e}")
	else:
		print("USBMonitor: no .cfg file found on drive.")


# Module-level setup — starts automatically on import
_context = pyudev.Context()
_monitor = pyudev.Monitor.from_netlink(_context)
_monitor.filter_by(subsystem='block')

threading.Thread(target=_watch, daemon=True).start()
_check_mounted()

print("USBMonitor: initialized.")


if __name__ == "__main__":
	print("Watching for USB events... plug/unplug drive.")
	while True:
		time.sleep(1)
