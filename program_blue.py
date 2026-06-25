#!/usr/bin/env python3
"""
program_blue.py

Interface for communicating with ProgramBlue animatronic software over
RS-232 via a USB-to-serial adapter (PL2303 or similar).

Hardware path:
	PC (ProgramBlue) → USB-RS232 → DB9 null-modem → DB9 → USB-serial → Jetson Orin Nano

Protocol (reverse-engineered from USBPcap capture):
	- Frame length : 38 bytes
	- Byte  [0]    : 0xAB (start marker)
	- Bytes [1-2]  : channel bitmask (16 channels, MSB first)
	                   byte[1] bit7 = channel 0
	                   byte[1] bit6 = channel 1
	                   ...
	                   byte[1] bit0 = channel 7
	                   byte[2] bit7 = channel 8
	                   byte[2] bit6 = channel 9
	                   ...
	- Byte  [26]   : 0x40 (fixed flags byte)
	- Bytes [3-25] : currently observed as zero (reserved / unknown)
	- Bytes [27-37]: currently observed as zero (reserved / unknown)

Handshake (reverse-engineered from USBPcap capture):
	ProgramBlue sends single-byte commands before and during playback.
	The board must respond or ProgramBlue will not proceed.

	CMD 'Y' (0x59) — identification query
	  → respond: "SP2" (0x53 0x50 0x32)

	CMD 'W' (0x57) — status/version query
	  → respond: 0x00 0x00 0x6C 0x4D

	CMD 'M' (0x4D) — start/continue status stream
	  → respond: 0x18 0x00 0x00, then keep streaming at STREAM_HZ

	Command bytes are only intercepted when no partial frame is being
	assembled, preventing false-positives inside 0xAB frames.

Dispatched signals:
	"onProgramBlueEvent" — fired for each channel whose state changed;
	                       kwargs: channel (int, 0-15), val (int, 0 or 1)
"""

import threading
import time
import serial
import re
from typing import Optional
from pydispatch import dispatcher


# ─── Hardware Config ──────────────────────────────────────────────────────────

SERIAL_PORT    = "/dev/ttyUSB0"	# PL2303 USB-serial adapter on Jetson
BAUD_RATE      = 115200
POLL_TIMEOUT   = 0.1			# Serial read timeout in seconds


# ─── ProgramBlue Protocol ─────────────────────────────────────────────────────

FRAME_LENGTH  = 38		# Total frame size in bytes
FRAME_START   = 0xAB	# Start-of-frame marker (byte 0)
FRAME_FLAGS   = 0x40	# Fixed value always present at byte 26
NUM_CHANNELS  = 16		# Channels encoded as bits across bytes 1-2


# ─── ProgramBlue Handshake Commands ──────────────────────────────────────────

CMD_IDENTIFY = 0x59		# 'Y' — identification query
CMD_STATUS   = 0x57		# 'W' — status/version query
CMD_STREAM   = 0x4D		# 'M' — start/continue status stream

IDENTIFY_RESP = bytes([0x53, 0x50, 0x32])		# "SP2"
STATUS_RESP   = bytes([0x00, 0x00, 0x6C, 0x4D])
STREAM_RESP   = bytes([0x18, 0x00, 0x00])

STREAM_HZ     = 50		# Status stream rate in Hz while streaming mode is active


class ProgramBlue:
	def __init__(self, port: str = SERIAL_PORT) -> None:
		self._port = port
		self._ser: Optional[serial.Serial] = None
		self._rx_buf: bytearray = bytearray()
		self._stop_event = threading.Event()
		self._tx_lock = threading.Lock()
		self._handshake_lock = threading.Lock()
		self._tx_bitmask: int = 0
		self._available: bool = False
		self._streaming: bool = False

		# Track last known channel states to only dispatch on changes
		self._channel_states: list[int] = [0] * NUM_CHANNELS

		try:
			self._ser = serial.Serial(
				port=port,
				baudrate=BAUD_RATE,
				bytesize=serial.EIGHTBITS,
				parity=serial.PARITY_NONE,
				stopbits=serial.STOPBITS_ONE,
				timeout=POLL_TIMEOUT,
				# Do not enable hardware flow control — the null-modem adapter
				# crosses RTS/CTS, and ProgramBlue doesn't require flow control.
				rtscts=False,
				dsrdtr=False,
			)
			# Drive RTS low so the null-modem's CTS line is satisfied,
			# mirroring what the SC16IS752 MCR setup did on the old board.
			self._ser.rts = True
			self._available = True
			print(f"ProgramBlue: opened {port} at {BAUD_RATE} baud, 8N1.")
		except serial.SerialException as e:
			print(f"ProgramBlue: could not open {port} — {e}. Running in offline mode.")

		self._reader_thread = threading.Thread(
			target=self._reader_loop, name="programblue-reader", daemon=True
		)
		self._stream_thread = threading.Thread(
			target=self._stream_loop, name="programblue-stream", daemon=True
		)
		self._reader_thread.start()
		self._stream_thread.start()
		print("ProgramBlue: reader and stream threads started.")

	# ─── Public API ──────────────────────────────────────────────────────────

	def send(self, data: bytes) -> None:
		"""Write bytes to ProgramBlue over the USB-serial TX line."""
		if not self._available or self._ser is None:
			return
		with self._tx_lock:
			self._ser.write(data)
			self._ser.flush()

	def send_channel(self, channel: int, val: int) -> None:
		"""Update a single channel in the outgoing bitmask and send a frame."""
		if not self._available:
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

	def stop(self) -> None:
		self._stop_event.set()
		self._reader_thread.join(timeout=2)
		self._stream_thread.join(timeout=2)
		if self._ser and self._ser.is_open:
			self._ser.close()
		print("ProgramBlue: stopped.")

	# ─── Reader Loop ─────────────────────────────────────────────────────────

	def _reader_loop(self) -> None:
		print("ProgramBlue: listening for data...")
		while not self._stop_event.is_set():
			if not self._available or self._ser is None:
				time.sleep(0.1)
				continue
			try:
				# Blocking read with POLL_TIMEOUT; returns b"" on timeout
				raw = self._ser.read(1)
				if raw:
					self._handle_byte(raw[0])
			except serial.SerialException as e:
				print(f"ProgramBlue: serial error — {e}. Retrying...")
				time.sleep(0.1)

	def _handle_byte(self, byte: int) -> None:
		"""Route incoming byte: handle command bytes or accumulate for frame parsing.

		Command bytes are only intercepted when no partial frame is being
		assembled, preventing 0x4D / 0x57 / 0x59 inside a frame from being
		mistakenly treated as commands.
		"""
		if not self._rx_buf and byte in (CMD_IDENTIFY, CMD_STATUS, CMD_STREAM):
			self._handle_command(byte)
			return
		self._rx_buf.append(byte)
		self._try_parse_frame()

	def _handle_command(self, cmd: int) -> None:
		"""Respond to ProgramBlue handshake commands."""
		if cmd == CMD_IDENTIFY:
			print("ProgramBlue: received 'Y' — sending SP2 identification.")
			with self._handshake_lock:
				self._streaming = False
				self._rx_buf.clear()
				if self._ser:
					self._ser.reset_input_buffer()
					self._ser.reset_output_buffer()
				self.send(IDENTIFY_RESP)
		elif cmd == CMD_STATUS:
			with self._handshake_lock:
				self.send(STATUS_RESP)
		elif cmd == CMD_STREAM:
			with self._handshake_lock:
				self._streaming = True
				self.send(STREAM_RESP)

	def _try_parse_frame(self) -> None:
		"""Discard bytes before start marker, then parse complete frames."""
		while self._rx_buf and self._rx_buf[0] != FRAME_START:
			discarded = self._rx_buf.pop(0)
			print(f"ProgramBlue: discarding out-of-frame byte 0x{discarded:02X}")

		if len(self._rx_buf) >= FRAME_LENGTH:
			frame = bytearray(self._rx_buf[:FRAME_LENGTH])
			self._rx_buf = self._rx_buf[FRAME_LENGTH:]
			self._dispatch_frame(frame)

	def _dispatch_frame(self, frame: bytearray) -> None:
		"""Parse channel bitmask and dispatch signals for any state changes."""
		bitmask = (frame[1] << 8) | frame[2]

		for ch in range(NUM_CHANNELS):
			active = int(bool(bitmask & (0x8000 >> ch)))
			if active != self._channel_states[ch]:
				self._channel_states[ch] = active
				dispatcher.send(signal="onProgramBlueEvent", channel=ch, val=active)

	# ─── Status Stream Loop ───────────────────────────────────────────────────

	def _stream_loop(self) -> None:
		"""Continuously send STREAM_RESP at STREAM_HZ while streaming mode is active.

		ProgramBlue sends 'M' to start the status stream and expects the
		board to keep broadcasting 0x18 0x00 0x00. This mirrors the BlueSpider's
		behaviour observed in the USBPcap capture.
		"""
		interval = 1.0 / STREAM_HZ
		while not self._stop_event.is_set():
			with self._handshake_lock:
				if self._streaming and self._available:
					self.send(STREAM_RESP)
			time.sleep(interval)


# ─── Standalone .shw File Parser ─────────────────────────────────────────────

def parse_file(file: str, fps: int = 40) -> tuple[str, list[list]]:
	"""Parse a ProgramBlue .shw file and return (audio_path, channel_events).

	Supports both v2 (DSFRobots) and v5 (dsfa) file formats.
	Returns a list of events in the form [timestamp_ms, channel, value].
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
		NUM_CHANNELS = 256

		table_end = decoded.find(b"<", 512)
		if table_end == -1:
			raise ValueError("Could not find v5 frame table terminator")

		frame_count = (table_end - FRAME_BASE) // FRAME_STRIDE
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
			if pos == 257:
				return 1
			if 0 <= pos <= 254:
				return pos + 2
			return None

		events: list[list] = []
		prev = [0] * NUM_CHANNELS

		for frame in range(frame_count):
			row_start = FRAME_BASE + frame * FRAME_STRIDE
			row = decoded[row_start:row_start + FRAME_STRIDE]

			if len(row) < FRAME_STRIDE:
				break

			current = [0] * NUM_CHANNELS

			for pos, b in enumerate(row):
				channel = pos_to_channel(pos)
				if channel is not None:
					current[channel - 1] = 1 if b else 0

			for channel_index, value in enumerate(current):
				if value != prev[channel_index]:
					events.append([
						frame_to_ms(frame, fps),
						channel_index + 1,
						value,
					])

			prev = current

		print(
			f"ProgramBlue: v5 layout frames={frame_count}, "
			f"stride={FRAME_STRIDE}, base={FRAME_BASE}, channels={NUM_CHANNELS}"
		)

		return events

	def parse_v2_frame_table(
		decoded: bytes,
		fps: int,
		body_start: int,
		body_end: int,
	) -> list[list]:
		NUM_CHANNELS = 256
		frame_lines = decoded[body_start:body_end].splitlines()

		events: list[list] = []
		prev = [0] * NUM_CHANNELS

		for frame, line in enumerate(frame_lines):
			if len(line) != 256:
				continue

			row = bytes.fromhex(line.decode("ascii"))
			current = [0] * NUM_CHANNELS

			for byte_index, value in enumerate(row):
				current[byte_index * 2] = 1 if value & 0x10 else 0
				current[byte_index * 2 + 1] = 1 if value & 0x01 else 0

			for channel_index, value in enumerate(current):
				if value != prev[channel_index]:
					events.append([
						frame_to_ms(frame, fps),
						channel_index + 1,
						value,
					])

			prev = current

		print(
			f"ProgramBlue: v2 layout frames={len(frame_lines)}, "
			f"channels={NUM_CHANNELS}"
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
			time.sleep(1)
	except KeyboardInterrupt:
		pb.stop()
		print("Exiting.")