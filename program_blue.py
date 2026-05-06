# About 123 frames long
# Frame 0 = channel 1 on, frame 40 channel 1 off
# frame 48 = channel 2 on, frame 72 channel ff

#!/usr/bin/env python3
"""
program_blue.py

Interface for communicating with ProgramBlue animatronic software over
RS-232 via an SC16IS752 I2C-to-UART bridge.

Hardware path:
	PC (ProgramBlue) → USB-RS232 → DB9 → MAX3232 → SC16IS752 → I2C1 → Jetson Orin Nano

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

Dispatched signals:
	"onProgramBlueEvent" — fired for each channel whose state changed;
	                       kwargs: channel (int, 0-15), val (int, 0 or 1)
"""

import threading
import time
import smbus2
import struct
import os
import re
from typing import Optional, Any
from pydispatch import dispatcher


# ─── SC16IS752 Register Map ───────────────────────────────────────────────────

CH_A = 0b00		# UART channel A — RS-232 / ProgramBlue

def _reg(reg: int, ch: int = CH_A) -> int:
	"""Build SC16IS752 subaddress byte: reg[3:0] << 3 | ch << 1."""
	return (reg << 3) | (ch << 1)

REG_RHR  = 0x00	# Receive Holding Register  (read)
REG_THR  = 0x00	# Transmit Holding Register (write)
REG_FCR  = 0x02	# FIFO Control Register     (write)
REG_LCR  = 0x03	# Line Control Register
REG_LSR  = 0x05	# Line Status Register
REG_DLL  = 0x00	# Divisor Latch Low  (when LCR[7]=1)
REG_DLH  = 0x01	# Divisor Latch High (when LCR[7]=1)

LSR_DATA_READY    = 0x01
LSR_OVERRUN_ERROR = 0x02
LSR_THR_EMPTY     = 0x20


# ─── Hardware Config ──────────────────────────────────────────────────────────

I2C_BUS        = 1		# I2C1 on Jetson Orin Nano 40-pin header
SC16IS752_ADDR = 0x48	# A0=GND, A1=GND — no conflict with MCP23008 @ 0x20/0x21
BAUD_RATE      = 115200
CRYSTAL_HZ     = 1_843_200	# 1.8432 MHz — gives exact 115200 baud with divisor=1
POLL_INTERVAL  = 0.002		# 2 ms (~500 Hz)


# ─── ProgramBlue Protocol ─────────────────────────────────────────────────────

FRAME_LENGTH  = 38		# Total frame size in bytes
FRAME_START   = 0xAB	# Start-of-frame marker (byte 0)
FRAME_FLAGS   = 0x40	# Fixed value always present at byte 26
NUM_CHANNELS  = 16		# Channels encoded as bits across bytes 1-2


class ProgramBlue:
	def __init__(self) -> None:
		self._bus = smbus2.SMBus(I2C_BUS)
		self._rx_buf: bytearray = bytearray()
		self._stop_event = threading.Event()
		self._tx_lock = threading.Lock()
		self._tx_bitmask: int = 0
		self._available: bool = False

		# Track last known channel states to only dispatch on changes
		self._channel_states: list[int] = [0] * NUM_CHANNELS

		try:
			self._configure_uart()
			self._available = True
		except OSError as e:
			print(f"ProgramBlue: SC16IS752 not found on I2C — {e}. Running in offline mode.")

		self._thread = threading.Thread(target=self._reader_loop, name="programblue-reader", daemon=True)
		self._thread.start()
		print("ProgramBlue: reader started.")

	# ─── Public API ──────────────────────────────────────────────────────────

	def send(self, data: bytes) -> None:
		"""Write bytes to ProgramBlue over the SC16IS752 TX FIFO."""
		if not self._available:
			return
		with self._tx_lock:
			for byte in data:
				while not (self._read_reg(REG_LSR) & LSR_THR_EMPTY):
					time.sleep(0.0001)
				self._write_reg(REG_THR, byte)

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
		self._thread.join(timeout=2)
		self._bus.close()
		print("ProgramBlue: stopped.")

	def parse_file(self, file: str) -> tuple[str, list[list]]:
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
	        FRAME_BASE = 20
	        FRAME_STRIDE = 258
	        NUM_CHANNELS = 256

	        table_end = decoded.find(b"<", 512)
	        if table_end == -1:
	            raise ValueError("Could not find v5 frame table terminator")

	        frame_count = (table_end - FRAME_BASE) // FRAME_STRIDE
	        if frame_count <= 0:
	            raise ValueError("Could not detect v5 frame table")

	        # Trim trailing blank/footer rows.
	        while frame_count > 0:
	            row_start = FRAME_BASE + (frame_count - 1) * FRAME_STRIDE
	            row = decoded[row_start : row_start + FRAME_STRIDE]
	            channel_bytes = row[:255] + row[257:258]
	            if any(channel_bytes):
	                break
	            frame_count -= 1

	        # Some v5 files have a trailing footer byte that looks like channel 1.
	        if frame_count > 0:
	            row_start = FRAME_BASE + (frame_count - 1) * FRAME_STRIDE
	            row = decoded[row_start : row_start + FRAME_STRIDE]
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
	            row = decoded[row_start : row_start + FRAME_STRIDE]

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
	            audio_size = int(m.group(1))
	            audio_offset = m.end()
	            audio_end = audio_offset + audio_size

	            with open(AUDIO_TMP, "wb") as f:
	                f.write(data[audio_offset:audio_end])

	            meta = trim_v5_version(data[audio_end:])
	            decoded_meta = decode_v5_metadata(meta)
	            fps = int(getattr(self, "fps", 40))
	            events = parse_v5_frame_table(decoded_meta, fps)

	            print(f"ProgramBlue: audio extracted to {AUDIO_TMP} ({audio_size} bytes)")
	            print(f"ProgramBlue: parsed {len(events)} channel events from '{file}'")
	            return AUDIO_TMP, events

	        decoded = bytes((b - 54) & 0xFF for b in data)
	        data_marker = b"\r\n<DSFROBOTSDATA>\r\n"
	        data_start = decoded.find(data_marker)

	        if data_start == -1:
	            raise ValueError("Unknown .shw format")

	        with open(AUDIO_TMP, "wb") as f:
	            f.write(decoded[:data_start])

	        body_start = data_start + len(data_marker)
	        body_end = decoded.find(b"\r\n</DSFROBOTSDATA>", body_start)

	        if body_end == -1:
	            raise ValueError("Missing </DSFROBOTSDATA> marker")

	        fps_match = re.search(rb"\bFPS=(\d+)", decoded[body_end:])
	        fps = int(fps_match.group(1)) if fps_match else int(getattr(self, "fps", 40))
	        events = parse_v2_frame_table(decoded, fps, body_start, body_end)

	        print(f"ProgramBlue: audio extracted to {AUDIO_TMP} ({data_start} bytes)")
	        print(f"ProgramBlue: parsed {len(events)} channel events from '{file}'")
	        return AUDIO_TMP, events

	    except Exception as e:
	        print(f"ProgramBlue: failed to parse '{file}': {e}")
	        return AUDIO_TMP, []

	# ─── Reader Loop ─────────────────────────────────────────────────────────

	def _reader_loop(self) -> None:
		print("ProgramBlue: listening for data...")
		while not self._stop_event.is_set():
			if self._available:
				try:
					byte = self._read_byte()
					if byte is not None:
						self._rx_buf.append(byte)
						self._try_parse_frame()
				except OSError as e:
					print(f"ProgramBlue: I2C error — {e}. Retrying...")
					time.sleep(0.1)

			time.sleep(POLL_INTERVAL)

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
		# Channels are packed as bits across bytes 1 and 2, MSB first
		bitmask = (frame[1] << 8) | frame[2]

		for ch in range(NUM_CHANNELS):
			active = int(bool(bitmask & (0x8000 >> ch)))
			if active != self._channel_states[ch]:
				self._channel_states[ch] = active
				dispatcher.send(signal="onProgramBlueEvent", channel=ch, val=active)

	# ─── SC16IS752 Helpers ───────────────────────────────────────────────────

	def _configure_uart(self) -> None:
		"""Initialise SC16IS752 channel A for 115200 8N1 with FIFOs enabled."""
		self._write_reg(REG_LCR, 0x80)		# Enable divisor latch
		divisor = CRYSTAL_HZ // (BAUD_RATE * 16)
		self._write_reg(REG_DLL, divisor & 0xFF)
		self._write_reg(REG_DLH, (divisor >> 8) & 0xFF)
		self._write_reg(REG_LCR, 0x03)		# 8N1, disable latch
		self._write_reg(REG_FCR, 0x07)		# Enable + reset TX/RX FIFOs
		print(f"ProgramBlue: SC16IS752 configured — {BAUD_RATE} baud, 8N1.")

	def _write_reg(self, reg: int, value: int, ch: int = CH_A) -> None:
		self._bus.write_byte_data(SC16IS752_ADDR, _reg(reg, ch), value)

	def _read_reg(self, reg: int, ch: int = CH_A) -> int:
		return self._bus.read_byte_data(SC16IS752_ADDR, _reg(reg, ch))

	def _read_byte(self) -> Optional[int]:
		lsr = self._read_reg(REG_LSR)
		if lsr & LSR_OVERRUN_ERROR:
			print("ProgramBlue: RX overrun — data lost.")
		if lsr & LSR_DATA_READY:
			return self._read_reg(REG_RHR)
		return None

	def __del__(self):
		self.stop()


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
