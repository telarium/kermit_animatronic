import os
import subprocess
import threading
import time
from pydispatch import dispatcher
from typing import Optional, List

GADGET_ROOT   = "/sys/kernel/config/usb_gadget/animatronic"
GADGET_UDC    = f"{GADGET_ROOT}/UDC"
RAW_MIDI_GLOB = "/dev/snd/midiC"   # we scan for midiC*D0 matching f_midi card
PORT_KEYWORDS = ("f_midi", "animatronic")

MODULES = ("libcomposite", "usb_f_midi")


class MIDI:
	def __init__(self, device: Optional[str] = None) -> None:
		self._device:   Optional[str]               = None
		self._rx_fd:    Optional[int]               = None
		self._tx_fd:    Optional[int]               = None
		self._rx_thread: Optional[threading.Thread] = None
		self._stop_rx   = threading.Event()

		if not self._setup_gadget():
			print("MIDI: gadget setup failed — MIDI unavailable.")
			return

		dev = device or self._find_device()
		if not dev:
			print("MIDI: no raw MIDI device found — MIDI unavailable.")
			return

		try:
			# Open the same device node for both read and write.
			self._rx_fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
			self._tx_fd = os.open(dev, os.O_WRONLY)
			self._device = dev
			print(f"MIDI: opened {dev}")
		except OSError as e:
			print(f"MIDI: failed to open {dev} — {e}")
			return

		self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
		self._rx_thread.start()

	# ------------------------------------------------------------------ #
	#  Device discovery                                                    #
	# ------------------------------------------------------------------ #

	def _find_device(self) -> Optional[str]:
		"""Scan /proc/asound/cards for the f_midi card and return its
		/dev/snd/midiC*D0 path."""
		try:
			cards = open("/proc/asound/cards").read()
		except OSError:
			return None

		for line in cards.splitlines():
			lower = line.lower()
			if any(kw in lower for kw in PORT_KEYWORDS):
				# Line format: "  3 [Animatronic   ]: ..."
				parts = line.split()
				if parts:
					card_num = parts[0]
					dev = f"/dev/snd/midiC{card_num}D0"
					if os.path.exists(dev):
						print(f"MIDI: found device at {dev} (card {card_num})")
						return dev
		return None

	# ------------------------------------------------------------------ #
	#  Gadget setup                                                        #
	# ------------------------------------------------------------------ #

	def _setup_gadget(self) -> bool:
		"""Load kernel modules and configure the Animatronic MIDI USB gadget
		via configfs. Returns True if the gadget is active."""

		if self._gadget_active():
			print("MIDI: gadget already active.")
			return True

		self._teardown_gadget()

		for mod in MODULES:
			try:
				subprocess.check_call(
					["modprobe", mod],
					stdout=subprocess.DEVNULL,
					stderr=subprocess.DEVNULL,
				)
			except subprocess.CalledProcessError as e:
				print(f"MIDI: modprobe {mod} failed — {e}")
				print("MIDI: is usb_f_midi.ko installed? Run setup.py first.")
				return False

		try:
			G = GADGET_ROOT

			def w(path: str, value: str) -> None:
				with open(path, "w") as f:
					f.write(value)

			os.makedirs(G, exist_ok=True)
			w(f"{G}/idVendor",  "0x1d6b")
			w(f"{G}/idProduct", "0x0486")
			w(f"{G}/bcdDevice", "0x0100")
			w(f"{G}/bcdUSB",    "0x0200")

			os.makedirs(f"{G}/strings/0x409", exist_ok=True)
			w(f"{G}/strings/0x409/manufacturer", "Andrew")
			w(f"{G}/strings/0x409/product",      "Animatronic MIDI")
			w(f"{G}/strings/0x409/serialnumber",  "kermit-0001")

			os.makedirs(f"{G}/configs/c.1/strings/0x409", exist_ok=True)
			w(f"{G}/configs/c.1/strings/0x409/configuration", "MIDI")
			w(f"{G}/configs/c.1/MaxPower", "250")

			os.makedirs(f"{G}/functions/midi.usb0", exist_ok=True)
			w(f"{G}/functions/midi.usb0/id", "Animatronic")

			link = f"{G}/configs/c.1/midi.usb0"
			if not os.path.exists(link):
				os.symlink(f"{G}/functions/midi.usb0", link)

			udc_names = os.listdir("/sys/class/udc")
			if not udc_names:
				print("MIDI: no UDC found — is the USB-C port available?")
				return False

			w(f"{G}/UDC", udc_names[0])

		except OSError as e:
			print(f"MIDI: configfs setup failed — {e}")
			return False

		time.sleep(0.5)

		if not self._gadget_active():
			print("MIDI: gadget configured but UDC did not bind.")
			return False

		print(f"MIDI: gadget up on {udc_names[0]}.")
		return True

	def _teardown_gadget(self) -> None:
		"""Remove any partial configfs gadget tree left from a failed setup."""
		G = GADGET_ROOT
		if not os.path.exists(G):
			return
		try:
			udc = f"{G}/UDC"
			if os.path.exists(udc):
				with open(udc, "w") as f:
					f.write("")
			for config in _listdir(f"{G}/configs"):
				config_path = f"{G}/configs/{config}"
				for entry in _listdir(config_path):
					entry_path = f"{config_path}/{entry}"
					if os.path.islink(entry_path):
						os.remove(entry_path)
				for strings_dir in _listdir(f"{config_path}/strings"):
					_rmdir(f"{config_path}/strings/{strings_dir}")
				_rmdir(f"{config_path}/strings")
				_rmdir(config_path)
			_rmdir(f"{G}/configs")
			for fn in _listdir(f"{G}/functions"):
				_rmdir(f"{G}/functions/{fn}")
			_rmdir(f"{G}/functions")
			for lang in _listdir(f"{G}/strings"):
				_rmdir(f"{G}/strings/{lang}")
			_rmdir(f"{G}/strings")
			_rmdir(G)
		except OSError as e:
			print(f"MIDI: teardown warning — {e}")

	def _gadget_active(self) -> bool:
		try:
			return bool(open(GADGET_UDC).read().strip())
		except OSError:
			return False

	# ------------------------------------------------------------------ #
	#  Receive loop                                                        #
	# ------------------------------------------------------------------ #

	def _rx_loop(self) -> None:
		"""Background thread: read raw MIDI bytes and dispatch messages."""
		import select
		buf = bytearray()
		while not self._stop_rx.is_set():
			try:
				ready, _, _ = select.select([self._rx_fd], [], [], 0.1)
				if not ready:
					continue
				chunk = os.read(self._rx_fd, 64)
				if not chunk:
					continue
				buf.extend(chunk)
				buf = _parse_midi_bytes(buf, self._dispatch)
			except OSError:
				break

	def _dispatch(self, msg_type: str, note: int, value: int) -> None:
		dispatcher.send(signal="midi_input", msg_type=msg_type, note=note, value=value)

	# ------------------------------------------------------------------ #
	#  Send                                                                #
	# ------------------------------------------------------------------ #

	def send_message(self, note: int, value: int) -> None:
		"""Send a note-on (value=1) or note-off (value=0) message."""
		if self._tx_fd is None:
			return
		if value == 1:
			data = bytes([0x90, note & 0x7F, 127])
		else:
			data = bytes([0x80, note & 0x7F, 0])
		try:
			os.write(self._tx_fd, data)
			print(f"MIDI: sent {'note_on' if value == 1 else 'note_off'} note={note} velocity={127 if value == 1 else 0}")
		except OSError as e:
			print(f"MIDI: send failed — {e}")

	# ------------------------------------------------------------------ #
	#  Cleanup                                                             #
	# ------------------------------------------------------------------ #

	def close(self) -> None:
		self._stop_rx.set()
		if self._rx_thread:
			self._rx_thread.join(timeout=1.0)
		if self._rx_fd is not None:
			os.close(self._rx_fd)
			self._rx_fd = None
		if self._tx_fd is not None:
			os.close(self._tx_fd)
			self._tx_fd = None


# ------------------------------------------------------------------ #
#  Raw MIDI parsing                                                    #
# ------------------------------------------------------------------ #

def _parse_midi_bytes(buf: bytearray, callback) -> bytearray:
	"""Parse complete MIDI messages from buf, call callback for each,
	return any leftover incomplete bytes."""
	i = 0
	while i < len(buf):
		b = buf[i]
		if b & 0x80:
			msg_type = b & 0xF0
			# 3-byte messages: note on/off, aftertouch, control change, pitch bend
			if msg_type in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
				if i + 2 >= len(buf):
					break
				note  = buf[i + 1] & 0x7F
				value = buf[i + 2] & 0x7F
				if msg_type == 0x90 and value > 0:
					callback("note_on",  note, value)
				elif msg_type in (0x80,) or (msg_type == 0x90 and value == 0):
					callback("note_off", note, 0)
				# Other 3-byte messages silently consumed.
				i += 3
			# 2-byte messages: program change, channel pressure
			elif msg_type in (0xC0, 0xD0):
				if i + 1 >= len(buf):
					break
				i += 2
			# System messages
			elif b == 0xF0:
				# SysEx — skip to EOX
				end = buf.find(0xF7, i)
				if end == -1:
					break
				i = end + 1
			else:
				i += 1
		else:
			# Orphaned data byte — skip.
			i += 1
	return buf[i:]


# ------------------------------------------------------------------ #
#  Helpers                                                            #
# ------------------------------------------------------------------ #

def _listdir(path: str) -> List[str]:
	try:
		return os.listdir(path)
	except OSError:
		return []

def _rmdir(path: str) -> None:
	try:
		os.rmdir(path)
	except OSError:
		pass


# ------------------------------------------------------------------ #
#  File parsing                                                        #
# ------------------------------------------------------------------ #

def parse_file(file: str) -> List[List]:
	"""Parse a MIDI file and return a list of [time_ms, midi_note, value] events.
	value is 1 if velocity >= 90, 0 otherwise (note off or soft note on)."""
	import mido
	events: List[List] = []
	try:
		midi_file = mido.MidiFile(file)
		current_time_ms: float = 0.0
		for message in midi_file:
			current_time_ms += message.time * 1000
			if message.type == 'note_on':
				value = 1 if message.velocity >= 90 else 0
				events.append([current_time_ms, message.note, value])
			elif message.type == 'note_off':
				events.append([current_time_ms, message.note, 0])
	except Exception as e:
		print(f"MIDI: failed to parse '{file}': {e}")
	return events


# ------------------------------------------------------------------ #
#  Manual test                                                         #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
	midi = MIDI()
	if midi._tx_fd is not None:
		print("Sending test note (C4 on, then off)...")
		midi.send_message(60, 1)
		time.sleep(0.5)
		midi.send_message(60, 0)
	input("Press Enter to exit...\n")
	midi.close()
