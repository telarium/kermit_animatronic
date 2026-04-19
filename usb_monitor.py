import threading
import subprocess
import glob
import pyudev
from pydispatch import dispatcher
from typing import Optional

USB_MOUNT_POINT = "/mnt/usb"


class USBMonitor:
	def __init__(self) -> None:
		self._context = pyudev.Context()
		self._monitor = pyudev.Monitor.from_netlink(self._context)
		self._monitor.filter_by(subsystem='block')

		self._watch_thread = threading.Thread(target=self._watch, daemon=True)
		self._watch_thread.start()

		self._check_mounted()

		print("USBMonitor: initialized.")

	def get_mount_point(self) -> Optional[str]:
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

	def is_mounted(self) -> bool:
		"""Return True if a USB drive is currently mounted."""
		return self.get_mount_point() is not None

	def _is_usb_partition(self, device: pyudev.Device) -> bool:
		"""Return True if this device is a USB storage partition."""
		if device.device_type != 'partition':
			return False
		parent = device.find_parent('usb')
		return parent is not None

	def _watch(self) -> None:
		for device in iter(self._monitor.poll, None):
			if not self._is_usb_partition(device):
				continue

			device_name = device.sys_name

			if device.action == 'add':
				print(f"USBMonitor: drive connected ({device_name})")
				threading.Timer(2.0, self._check_mounted).start()

			elif device.action == 'remove':
				print(f"USBMonitor: drive removed ({device_name})")

	def _check_mounted(self) -> None:
		"""Check if the drive mounted successfully and print the result."""
		if self.is_mounted():
			print(f"USBMonitor: drive mounted at {USB_MOUNT_POINT}")
			self._look_for_config()
		else:
			print("USBMonitor: drive not yet mounted, retrying...")
			threading.Timer(2.0, self._check_mounted).start()

	def _look_for_config(self) -> None:
		"""Look for a .cfg file on the drive and dispatch its contents if found."""
		matches = glob.glob(f"{USB_MOUNT_POINT}/*.cfg")
		if matches:
			cfg_path = matches[0]
			print(f"USBMonitor: config file found: {cfg_path}")
			try:
				with open(cfg_path, 'r') as f:
					dispatcher.send(signal='usbConfigFound', path=cfg_path)
			except Exception as e:
				print(f"USBMonitor: error reading config file: {e}")
		else:
			print("USBMonitor: no .cfg file found on drive.")


# Example usage
if __name__ == "__main__":
	import time
	monitor = USBMonitor()
	print("Watching for USB events... plug/unplug drive.")
	while True:
		time.sleep(1)
