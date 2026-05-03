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
