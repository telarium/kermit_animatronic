#!/usr/bin/env python3
"""
program_blue.py

Interface for communicating with ProgramBlue animatronic software over
RS-232 via a USB-to-serial adapter (PL2303 or similar).

Hardware path:
	PC (ProgramBlue) → USB-RS232 → DB9 null-modem → DB9 → USB-serial → Jetson Orin Nano

Protocol:
	- Frame length : 38 bytes
	- Byte  [0]    : 0xAB (start marker)
	- Bytes [1-2]  : channel bitmask (16 channels, MSB first)
	                   byte[1] bit7 = channel 0, byte[2] bit0 = channel 15
	- Byte  [26]   : 0x40 (fixed flags byte)
	- Bytes [3-25], [27-37]: observed as zero (reserved / unknown)

	ProgramBlue's UI numbers channels from 1; the wire is 0-based. The
	character config uses wire numbering, so Mouth = 0.

Handshake:
	CMD 'Y' (0x59) — identification query    → "SP2"
	CMD 'W' (0x57) — status/version query    → 0x00 0x00 0x6C 0x4D
	CMD 'M' (0x4D) — start status stream     → 0x18 0x00 0x00, then keep
	                                           streaming at STREAM_HZ

	The stream is required — without it ProgramBlue's playback crawls.
	send() must not flush(): tcdrain blocks on USB-serial, and doing it
	under a lock the reader needs starves the reader thread.

	Command bytes are only intercepted when no partial frame is being
	assembled, preventing false-positives inside 0xAB frames.

Dispatched signals:
	"onProgramBlueEvent" — fired for each channel whose state changed;
	                       kwargs: channel (int, 0-15), val (int, 0 or 1)
"""

import re
import threading
import time
from typing import Optional

import serial
from pydispatch import dispatcher


# ─── Hardware Config ──────────────────────────────────────────────────────────

SERIAL_PORT    = "/dev/ttyUSB0"	# PL2303 USB-serial adapter on Jetson
BAUD_RATE      = 115200
POLL_TIMEOUT   = 0.05			# Serial read timeout in seconds


# ─── ProgramBlue Protocol ─────────────────────────────────────────────────────

FRAME_LENGTH  = 38		# Total frame size in bytes
FRAME_START   = 0xAB	# Start-of-frame marker (byte 0)
FRAME_FLAGS   = 0x40	# Fixed value always present at byte 26
NUM_CHANNELS  = 16		# Channels encoded as bits across bytes 1-2


# ─── ProgramBlue Handshake Commands ──────────────────────────────────────────

CMD_IDENTIFY = 0x59		# 'Y' — identification query
CMD_STATUS   = 0x57		# 'W' — status/version query
CMD_STREAM   = 0x4D		# 'M' — start/continue status stream

CMD_NAMES = {
	CMD_IDENTIFY: "Y identify",
	CMD_STATUS:   "W status",
	CMD_STREAM:   "M stream",
}

IDENTIFY_RESP = bytes([0x53, 0x50, 0x32])		# "SP2"
STATUS_RESP   = bytes([0x00, 0x00, 0x6C, 0x4D])
STREAM_RESP   = bytes([0x18, 0x00, 0x00])

STREAM_HZ     = 40		# Status stream rate; ProgramBlue paces its frame
						# output off this, so it must match SHW_FPS in the converter.

# ProgramBlue appears to be one-way for channel data. Sending frames upstream
# puts raw bytes in the buffer it reads handshake replies from.
RECONNECT_INTERVAL = 3.0	# Seconds between reopen attempts after a disconnect

ENABLE_TX_FRAMES = False


class ProgramBlue:
	def __init__(self, port: str = SERIAL_PORT) -> None:
		self._port = port
		self._ser: Optional[serial.Serial] = None
		self._rx_buf: bytearray = bytearray()
		self._stop_event = threading.Event()
		self._tx_lock = threading.Lock()
		self._tx_bitmask: int = 0
		self._available: bool = False
		self._streaming: bool = False
		self._reported_missing: bool = False

		self._frame_count: int = 0
		self._discard_count: int = 0
		self._cmd_counts: dict[int, int] = {c: 0 for c in CMD_NAMES}

		# Track last known channel states to only dispatch on changes
		self._channel_states: list[int] = [0] * NUM_CHANNELS

		self._open_port()

		self._reader_thread = threading.Thread(
			target=self._reader_loop, name="programblue-reader", daemon=True
		)
		self._stream_thread = threading.Thread(
			target=self._stream_loop, name="programblue-stream", daemon=True
		)
		self._reader_thread.start()
		self._stream_thread.start()
		print(f"ProgramBlue: reader and stream threads started ({STREAM_HZ} Hz).")

	# ─── Connection handling ─────────────────────────────────────────────────

	def _open_port(self) -> bool:
		"""Try to open the serial port. Quiet on repeated failures."""
		try:
			self._ser = serial.Serial(
				port=self._port,
				baudrate=BAUD_RATE,
				bytesize=serial.EIGHTBITS,
				parity=serial.PARITY_NONE,
				stopbits=serial.STOPBITS_ONE,
				timeout=POLL_TIMEOUT,
				rtscts=False,
				dsrdtr=False,
			)
			self._ser.rts = True
			self._ser.dtr = True
			self._available = True
			self._reported_missing = False
			print(f"ProgramBlue: opened {self._port} at {BAUD_RATE} baud, 8N1.")
			return True
		except (OSError, serial.SerialException) as e:
			self._ser = None
			self._available = False
			if not self._reported_missing:
				self._reported_missing = True
				print(f"ProgramBlue: could not open {self._port} — {e}. "
				      f"Retrying every {RECONNECT_INTERVAL:.0f}s.")
			return False

	def _handle_disconnect(self, why: str) -> None:
		"""Release every channel and drop the port so the reader can reopen it."""
		if self._available:
			print(f"ProgramBlue: disconnected — {why}")
		self._available = False
		self._streaming = False
		self._rx_buf.clear()
		# Release before anything else: a latched solenoid must not stay
		# energised because the cable came out.
		self._reset_channels()
		if self._ser is not None:
			try:
				self._ser.close()
			except Exception:
				pass
			self._ser = None

	# ─── Public API ──────────────────────────────────────────────────────────

	def send(self, data: bytes) -> None:
		"""Write bytes to ProgramBlue. No flush() — see module docstring."""
		if not self._available or self._ser is None:
			return
		try:
			with self._tx_lock:
				self._ser.write(data)
		except (OSError, serial.SerialException) as e:
			self._handle_disconnect(f"write failed: {e}")

	def send_channel(self, channel: int, val: int) -> None:
		"""Update a single channel in the outgoing bitmask and send a frame."""
		if not ENABLE_TX_FRAMES or not self._available:
			return
		# The config default is -1, and 0x8000 >> -1 raises ValueError.
		if not 0 <= channel < NUM_CHANNELS:
			return

		if val:
			self._tx_bitmask |= (0x8000 >> channel)
		else:
			self._tx_bitmask &= ~(0x8000 >> channel)

		frame = bytearray(FRAME_LENGTH)
		frame[0]  = FRAME_START
		frame[1]  = (self._tx_bitmask >> 8) & 0xFF
		frame[2]  = self._tx_bitmask & 0xFF
		frame[26] = FRAME_FLAGS
		self.send(bytes(frame))

	def stats(self) -> dict:
		return {
			"available":  self._available,
			"streaming":  self._streaming,
			"frames":     self._frame_count,
			"discarded":  self._discard_count,
			"commands":   {CMD_NAMES[c]: n for c, n in self._cmd_counts.items()},
			"channels":   list(self._channel_states),
			"partial_rx": len(self._rx_buf),
		}

	def stop(self) -> None:
		self._stop_event.set()
		self._reader_thread.join(timeout=2)
		self._stream_thread.join(timeout=2)
		self._handle_disconnect("shutting down")
		print(f"ProgramBlue: stopped. frames={self._frame_count} "
		      f"discarded={self._discard_count}")

	# ─── Reader Loop ─────────────────────────────────────────────────────────

	def _reader_loop(self) -> None:
		print("ProgramBlue: listening for data...")
		next_retry = 0.0
		while not self._stop_event.is_set():
			if not self._available or self._ser is None:
				now = time.monotonic()
				if now >= next_retry:
					next_retry = now + RECONNECT_INTERVAL
					self._open_port()
				time.sleep(0.1)
				continue
			try:
				waiting = self._ser.in_waiting
				raw = self._ser.read(waiting if waiting else 1)
				for b in raw:
					self._handle_byte(b)
			except (OSError, serial.SerialException, TypeError) as e:
				# TypeError covers _ser being cleared by another thread mid-read.
				self._handle_disconnect(f"read failed: {e}")
				next_retry = time.monotonic() + RECONNECT_INTERVAL

	def _handle_byte(self, byte: int) -> None:
		if not self._rx_buf and byte in CMD_NAMES:
			self._handle_command(byte)
			return
		self._rx_buf.append(byte)
		self._try_parse_frame()

	def _handle_command(self, cmd: int) -> None:
		# No reset_input_buffer() here: ProgramBlue sends its command and the
		# bytes that follow in one burst, and flushing discards them.
		self._cmd_counts[cmd] += 1
		print(f"ProgramBlue: {CMD_NAMES[cmd]} (#{self._cmd_counts[cmd]}) — replying.")

		if cmd == CMD_IDENTIFY:
			self._streaming = False
			self._rx_buf.clear()
			self._reset_channels()
			self.send(IDENTIFY_RESP)
		elif cmd == CMD_STATUS:
			self.send(STATUS_RESP)
		elif cmd == CMD_STREAM:
			self._streaming = True
			self.send(STREAM_RESP)

	def _reset_channels(self) -> None:
		"""Drop every channel so nothing stays latched."""
		for ch, state in enumerate(self._channel_states):
			if state:
				self._channel_states[ch] = 0
				dispatcher.send(signal="onProgramBlueEvent", channel=ch, val=0)

	def _try_parse_frame(self) -> None:
		"""Discard bytes before start marker, then parse complete frames."""
		while self._rx_buf and self._rx_buf[0] != FRAME_START:
			self._rx_buf.pop(0)
			self._discard_count += 1

		while len(self._rx_buf) >= FRAME_LENGTH:
			frame = bytes(self._rx_buf[:FRAME_LENGTH])
			self._rx_buf = self._rx_buf[FRAME_LENGTH:]
			self._dispatch_frame(frame)

	def _dispatch_frame(self, frame: bytes) -> None:
		"""Parse channel bitmask and dispatch signals for any state changes."""
		self._frame_count += 1
		bitmask = (frame[1] << 8) | frame[2]

		for ch in range(NUM_CHANNELS):
			active = int(bool(bitmask & (0x8000 >> ch)))
			if active != self._channel_states[ch]:
				self._channel_states[ch] = active
				dispatcher.send(signal="onProgramBlueEvent", channel=ch, val=active)

	# ─── Status Stream Loop ───────────────────────────────────────────────────

	def _stream_loop(self) -> None:
		"""Push STREAM_RESP at STREAM_HZ while streaming mode is active.

		Takes no lock the reader needs — that contention is what previously
		stalled frame reception.
		"""
		interval = 1.0 / STREAM_HZ
		while not self._stop_event.is_set():
			if self._streaming and self._available:
				self.send(STREAM_RESP)
			time.sleep(interval)


# ─── Standalone .shw File Parser ─────────────────────────────────────────────

def parse_file(file: str, fps: int = 40) -> tuple[str, list[list]]:
	"""Parse a ProgramBlue .shw file and return (audio_path, channel_events).

	Supports both v2 (DSFRobots) and v5 (dsfa) file formats.
	Returns events as [timestamp_ms, channel, value], in wire numbering
	(0-based) to match _dispatch_frame and the character config.
	"""
	AUDIO_TMP = "/tmp/shw_audio.mp3"

	def frame_to_ms(frame: int, fps: int | float) -> int:
		return round(frame * 1000.0 / fps)

	def decode_v5_metadata(meta: bytes) -> bytes:
		xor_key = bytes.fromhex(
			"b5ad97cb9ec6a3d103fdeaa5c8ccb3a0"
			"d0cc8dcec198cec1b8bdbad9c0949ad8cb"
		)
		return bytes(b ^ xor_key[i % len(xor_key)] for i, b in enumerate(meta))

	def trim_v5_version(meta: bytes) -> bytes:
		m = re.search(rb"v\d+\.\d+>$", meta)
		return meta[:m.start()] if m else meta

	def parse_v5_frame_table(decoded: bytes, fps: int) -> list[list]:
		FRAME_BASE   = 20
		FRAME_STRIDE = 258
		NUM_SHW_CHANNELS = 256

		table_end = decoded.find(b"<", 512)
		if table_end == -1:
			raise ValueError("Could not find v5 frame table terminator")

		frame_count = (table_end - FRAME_BASE) // FRAME_STRIDE
		# A clean table ends exactly on a row boundary. If it doesn't, the
		# last counted row runs into the trailer and isn't channel data.
		if (table_end - FRAME_BASE) % FRAME_STRIDE:
			frame_count -= 1
		if frame_count <= 0:
			raise ValueError("Could not detect v5 frame table")

		# Trim trailing blank/footer rows
		while frame_count > 0:
			row_start = FRAME_BASE + (frame_count - 1) * FRAME_STRIDE
			row = decoded[row_start:row_start + FRAME_STRIDE]
			channel_bytes = row[:255] + row[257:258]
			if any(channel_bytes):
				break
			frame_count -= 1

		# Some v5 files have a trailing footer byte that looks like channel 1
		if frame_count > 0:
			row_start = FRAME_BASE + (frame_count - 1) * FRAME_STRIDE
			row = decoded[row_start:row_start + FRAME_STRIDE]
			if len(row) == FRAME_STRIDE and row[257] and not any(row[:255]):
				frame_count -= 1

		def pos_to_channel(pos: int) -> int | None:
			"""Row position -> 1-based .shw channel."""
			if pos == 257:
				return 1
			if 0 <= pos <= 254:
				return pos + 2
			return None

		events: list[list] = []
		prev = [0] * NUM_SHW_CHANNELS

		for frame in range(frame_count):
			row_start = FRAME_BASE + frame * FRAME_STRIDE
			row = decoded[row_start:row_start + FRAME_STRIDE]

			if len(row) < FRAME_STRIDE:
				break

			current = [0] * NUM_SHW_CHANNELS

			for pos, b in enumerate(row):
				channel = pos_to_channel(pos)
				if channel is not None:
					current[channel - 1] = 1 if b else 0

			for channel_index, value in enumerate(current):
				if value != prev[channel_index]:
					# channel_index is 0-based = wire numbering
					events.append([
						frame_to_ms(frame, fps),
						channel_index,
						value,
					])

			prev = current

		print(
			f"ProgramBlue: v5 layout frames={frame_count}, "
			f"stride={FRAME_STRIDE}, base={FRAME_BASE}, channels={NUM_SHW_CHANNELS}"
		)

		return events

	def parse_v2_frame_table(
		decoded: bytes,
		fps: int,
		body_start: int,
		body_end: int,
	) -> list[list]:
		NUM_SHW_CHANNELS = 256
		frame_lines = decoded[body_start:body_end].splitlines()

		events: list[list] = []
		prev = [0] * NUM_SHW_CHANNELS

		for frame, line in enumerate(frame_lines):
			if len(line) != 256:
				continue

			row = bytes.fromhex(line.decode("ascii"))
			current = [0] * NUM_SHW_CHANNELS

			for byte_index, value in enumerate(row):
				current[byte_index * 2] = 1 if value & 0x10 else 0
				current[byte_index * 2 + 1] = 1 if value & 0x01 else 0

			for channel_index, value in enumerate(current):
				if value != prev[channel_index]:
					events.append([
						frame_to_ms(frame, fps),
						channel_index,
						value,
					])

			prev = current

		print(
			f"ProgramBlue: v2 layout frames={len(frame_lines)}, "
			f"channels={NUM_SHW_CHANNELS}"
		)

		return events

	try:
		with open(file, "rb") as f:
			data = f.read()

		m = re.match(rb"^(\d+)<dsfa>", data)
		if m:
			audio_size   = int(m.group(1))
			audio_offset = m.end()
			audio_end    = audio_offset + audio_size

			with open(AUDIO_TMP, "wb") as f:
				f.write(data[audio_offset:audio_end])

			meta         = trim_v5_version(data[audio_end:])
			decoded_meta = decode_v5_metadata(meta)
			events       = parse_v5_frame_table(decoded_meta, fps)

			print(f"ProgramBlue: audio extracted to {AUDIO_TMP} ({audio_size} bytes)")
			print(f"ProgramBlue: parsed {len(events)} channel events from '{file}'")
			return AUDIO_TMP, events

		decoded     = bytes((b - 54) & 0xFF for b in data)
		data_marker = b"\r\n<DSFROBOTSDATA>\r\n"
		data_start  = decoded.find(data_marker)

		if data_start == -1:
			raise ValueError("Unknown .shw format")

		with open(AUDIO_TMP, "wb") as f:
			f.write(decoded[:data_start])

		body_start = data_start + len(data_marker)
		body_end   = decoded.find(b"\r\n</DSFROBOTSDATA>", body_start)

		if body_end == -1:
			raise ValueError("Missing </DSFROBOTSDATA> marker")

		fps_match = re.search(rb"\bFPS=(\d+)", decoded[body_end:])
		fps       = int(fps_match.group(1)) if fps_match else fps
		events    = parse_v2_frame_table(decoded, fps, body_start, body_end)

		print(f"ProgramBlue: audio extracted to {AUDIO_TMP} ({data_start} bytes)")
		print(f"ProgramBlue: parsed {len(events)} channel events from '{file}'")
		return AUDIO_TMP, events

	except Exception as e:
		print(f"ProgramBlue: failed to parse '{file}': {e}")
		return AUDIO_TMP, []


if __name__ == "__main__":

	def on_event(channel, val, **kwargs):
		state = "ON " if val else "OFF"
		print(f"  Channel {channel:2d}: {state}")

	dispatcher.connect(on_event, signal="onProgramBlueEvent")

	pb = ProgramBlue()

	try:
		while True:
			time.sleep(5)
			s = pb.stats()
			print(f"  [stats] frames={s['frames']} streaming={s['streaming']} "
			      f"discarded={s['discarded']} partial={s['partial_rx']} "
			      f"cmds={s['commands']}")
	except KeyboardInterrupt:
		pb.stop()
		print("Exiting.")
