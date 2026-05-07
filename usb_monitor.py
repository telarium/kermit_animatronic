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


def init_respeaker() -> None:
	"""Reboot ReSpeaker firmware to ensure a clean state at startup."""
	try:
		script_dir = os.path.dirname(os.path.abspath(__file__))
		xvf_py = os.path.join(script_dir, "lib", "respeaker", "python_control", "xvf_host.py")
		subprocess.run(["python3", xvf_py, "REBOOT", "--values", "1"], check=True)
		print("ReSpeaker: rebooting...")

		# Wait for device to re-enumerate
		for attempt in range(20):
			result = subprocess.run(["lsusb"], capture_output=True, text=True)
			if any("respeaker" in line.lower() for line in result.stdout.splitlines()):
				print("ReSpeaker: reboot complete.")
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