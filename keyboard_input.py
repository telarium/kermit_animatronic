#!/usr/bin/env python3
"""
keyboard_input.py — movement control from an attached USB HID keyboard.

Reads /dev/input/event* through evdev rather than pygame: SDL only produces
key events for a focused window, and start.py runs headless with
SDL_VIDEODRIVER=dummy.

Dispatches "keyEvent" with key/val — the same signal web_io.py emits for the
web keypad — so Movement.on_key_event handles both identically.
"""

import selectors
import threading
import time
from typing import Dict, List, Optional, Set

from evdev import InputDevice, ecodes, list_devices
from pydispatch import dispatcher

# How often to look for newly attached keyboards.
RESCAN_SECONDS = 2.0

# evdev EV_KEY values.
VALUE_UP     = 0
VALUE_DOWN   = 1
VALUE_REPEAT = 2


def keycode_to_char(code: int) -> Optional[str]:
	"""Map an evdev keycode to the single character the character JSON uses
	as a movement `key`, or None for anything else (shift, enter, keypad)."""
	name = ecodes.KEY.get(code)
	# Aliased codes come back as a list of names.
	if isinstance(name, (list, tuple)):
		name = next((n for n in name if str(n).startswith("KEY_")), None)
	if not name or not str(name).startswith("KEY_"):
		return None
	char = str(name)[len("KEY_"):]
	return char.lower() if len(char) == 1 else None


def is_keyboard(device: InputDevice) -> bool:
	"""Identify by capability, not name — the gamepad also reports EV_KEY,
	and the HDMI/power pseudo-devices report a handful of keys each."""
	capabilities = device.capabilities()
	keys = capabilities.get(ecodes.EV_KEY, [])
	axes = capabilities.get(ecodes.EV_ABS, [])
	if ecodes.ABS_X in axes:
		return False
	return ecodes.KEY_A in keys and ecodes.KEY_Z in keys


class USBKeyboardReader:
	"""Watches every attached keyboard on one thread.

	A keyboard usually presents several event nodes (keys, consumer control,
	system control), and only one of them carries the letters — so all
	matching nodes are read together rather than picking one.
	"""

	def __init__(self) -> None:
		self._devices: Dict[str, InputDevice] = {}
		# Per device, the characters currently held down. Used to suppress
		# duplicate presses and to release everything if the device vanishes.
		self._held: Dict[str, Set[str]] = {}

		self._selector = selectors.DefaultSelector()
		self._lock = threading.RLock()
		self._stop_event = threading.Event()

		self._thread = threading.Thread(target=self._run, daemon=True)
		self._thread.start()
		print("USBKeyboardReader: started.")

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

	def devices(self) -> List[str]:
		"""Paths of the keyboards currently being read."""
		with self._lock:
			return sorted(self._devices)

	def stop(self) -> None:
		self._stop_event.set()
		if self._thread.is_alive():
			self._thread.join(timeout=2.0)
		with self._lock:
			for device in list(self._devices.values()):
				self._remove(device)
		self._selector.close()
		print("USBKeyboardReader: stopped.")

	# -------------------------------------------------------------------------
	# Internal: device management
	# -------------------------------------------------------------------------

	def _scan(self) -> None:
		try:
			paths = list_devices()
		except OSError as e:
			print(f"USBKeyboardReader: could not list input devices: {e}")
			return

		for path in paths:
			if path in self._devices:
				continue
			try:
				device = InputDevice(path)
			except OSError:
				continue
			if not is_keyboard(device):
				device.close()
				continue
			self._add(device)

	def _add(self, device: InputDevice) -> None:
		with self._lock:
			try:
				self._selector.register(device, selectors.EVENT_READ)
			except (KeyError, ValueError, OSError) as e:
				print(f"USBKeyboardReader: could not watch {device.path}: {e}")
				device.close()
				return
			self._devices[device.path] = device
			self._held[device.path] = set()

			# Take the keyboard exclusively so keystrokes don't also reach any
			# TTY. The kernel drops the grab if this process dies.
			try:
				device.grab()
			except OSError as e:
				print(f"USBKeyboardReader: could not grab {device.path}: {e}")

		print(f"USBKeyboardReader: connected {device.name} ({device.path})")

	def _remove(self, device: InputDevice) -> None:
		with self._lock:
			path = device.path
			if path not in self._devices:
				return

			# Release anything still held. A keyboard unplugged mid-press
			# would otherwise leave a solenoid energised, and several
			# movements have no max_sec watchdog behind them.
			for char in sorted(self._held.pop(path, set())):
				self._dispatch(char, 0)

			try:
				self._selector.unregister(device)
			except (KeyError, ValueError, OSError):
				pass
			self._devices.pop(path, None)

		for close in (device.ungrab, device.close):
			try:
				close()
			except Exception:
				pass
		print(f"USBKeyboardReader: disconnected {path}")

	# -------------------------------------------------------------------------
	# Internal: read loop
	# -------------------------------------------------------------------------

	def _run(self) -> None:
		next_scan = 0.0
		while not self._stop_event.is_set():
			now = time.monotonic()
			if now >= next_scan:
				self._scan()
				next_scan = now + RESCAN_SECONDS

			# Timeout rather than block forever, so _stop_event is honoured
			# and the rescan still runs with no keyboard attached.
			try:
				ready = self._selector.select(timeout=0.5)
			except OSError:
				continue

			for selector_key, _ in ready:
				self._read_device(selector_key.fileobj)

	def _read_device(self, device: InputDevice) -> None:
		try:
			for event in device.read():
				if event.type == ecodes.EV_KEY:
					self._handle_key(device, event)
		except BlockingIOError:
			return
		except OSError:
			self._remove(device)

	def _handle_key(self, device: InputDevice, event) -> None:
		# Autorepeat would re-send a press the movement is already holding.
		if event.value == VALUE_REPEAT:
			return

		char = keycode_to_char(event.code)
		if char is None:
			return

		with self._lock:
			held = self._held.get(device.path)
			if held is None:
				return
			if event.value == VALUE_DOWN:
				if char in held:
					return
				held.add(char)
			else:
				if char not in held:
					return
				held.discard(char)

		self._dispatch(char, 1 if event.value == VALUE_DOWN else 0)

	def _dispatch(self, char: str, val: int) -> None:
		dispatcher.send(signal="keyEvent", key=char, val=val)


if __name__ == "__main__":
	def on_key(key, val, **kwargs):
		print(f"  key={key!r} val={val}")

	dispatcher.connect(on_key, signal="keyEvent")

	reader = USBKeyboardReader()
	print("Type on an attached USB keyboard (Ctrl-C to exit)...")
	try:
		while True:
			time.sleep(1)
	except KeyboardInterrupt:
		reader.stop()
		print("Exiting.")
