import glob
import subprocess
import threading
import time
import pyudev
from pydispatch import dispatcher
from typing import Optional

# USB mass storage only. ReSpeaker bring-up used to live here too; it is now
# respeaker.py.
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
			# The mount lingers briefly after the device goes, so confirm
			# before telling the system to fall back to the local backup.
			threading.Timer(2.0, _check_unmounted).start()


# Bounded so a boot with no drive attached doesn't retry forever.
_MOUNT_RETRIES = 10


def _check_mounted(attempt: int = 1) -> None:
	"""Check if the drive mounted successfully and retry if not."""
	if is_mounted():
		print(f"USBMonitor: drive mounted at {USB_MOUNT_POINT}")
		_announce_drive()
	elif attempt < _MOUNT_RETRIES:
		print("USBMonitor: drive not yet mounted, retrying...")
		threading.Timer(2.0, _check_mounted, args=(attempt + 1,)).start()
	else:
		print("USBMonitor: no drive mounted.")


def _check_unmounted() -> None:
	if is_mounted():
		return
	try:
		dispatcher.send(signal='usbDetached')
	except Exception as e:
		print(f"USBMonitor: error dispatching detach: {e}")


def _announce_drive() -> None:
	"""Report the attached drive, along with any .cfg sitting at its root."""
	matches = sorted(glob.glob(f"{USB_MOUNT_POINT}/*.cfg"))
	cfg_path = matches[0] if matches else ""
	if cfg_path:
		print(f"USBMonitor: config file found: {cfg_path}")
	else:
		print("USBMonitor: no .cfg file found on drive.")
	try:
		dispatcher.send(signal='usbAttached', path=cfg_path)
	except Exception as e:
		print(f"USBMonitor: error dispatching drive attach: {e}")


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
