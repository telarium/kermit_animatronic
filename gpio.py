import time
from typing import Optional

import smbus

# MCP23008 Register Addresses
IODIR   = 0x00   # GPIO direction register
# GPPU   = 0x06    # Pull-up resistor register
GPIOREG = 0x09    # GPIO register
OLAT    = 0x0A    # Output latch register

# I2C failures are reported, but rate-limited per (pin, op) so a hard fault at
# frame rate can't flood the console and stall the reader threads.
ERROR_REPORT_INTERVAL = 5.0


class MCP23008:
	def __init__(self, bus: smbus.SMBus, address: int) -> None:
		self.bus = bus
		self.address = address
		self.available = False
		self._last_error_time: dict = {}
		self._error_counts: dict = {}
		self.init_device()

	def init_device(self) -> None:
		try:
			# Initialize all GPIO pins as outputs and set to LOW
			self.bus.write_byte_data(self.address, IODIR, 0x00)  # All pins as outputs
			self.bus.write_byte_data(self.address, OLAT, 0x00)   # All pins LOW
			self.available = True
			print(f"GPIO: MCP23008 ready at 0x{self.address:02X}")
		except Exception as e:
			self.available = False
			print(f"Warning! MCP23008 unavailable at I2C address "
			      f"0x{self.address:02X} — {type(e).__name__}: {e}")

	def _report_error(self, pin: int, op: str, e: Exception) -> None:
		key = (pin, op)
		self._error_counts[key] = self._error_counts.get(key, 0) + 1
		now = time.monotonic()
		if now - self._last_error_time.get(key, 0.0) < ERROR_REPORT_INTERVAL:
			return
		self._last_error_time[key] = now
		print(f"GPIO: 0x{self.address:02X} pin {pin} {op} FAILED "
		      f"({self._error_counts[key]}x) — {type(e).__name__}: {e}")

	def set_pin(self, pin: int, value: int) -> bool:
		"""Set one pin high or low. Returns True if the write succeeded."""
		try:
			current_value = self.bus.read_byte_data(self.address, OLAT)
		except Exception as e:
			self._report_error(pin, "OLAT read", e)
			return False

		if value:
			new_value = current_value | (1 << pin)		# Set bit
		else:
			new_value = current_value & ~(1 << pin)		# Clear bit

		try:
			self.bus.write_byte_data(self.address, OLAT, new_value)
		except Exception as e:
			self._report_error(pin, "OLAT write", e)
			return False

		return True

	def get_pin(self, pin: int) -> Optional[int]:
		"""Read one pin. Returns None if the read failed."""
		try:
			return (self.bus.read_byte_data(self.address, GPIOREG) >> pin) & 0x01
		except Exception as e:
			self._report_error(pin, "GPIO read", e)
			return None


class GPIO:
	def __init__(self) -> None:
		self.mcp_devices: Optional[list] = None
		try:
			bus = smbus.SMBus(1)  # Initialize I2C bus

			# I2C addresses of the MCP23008 devices
			i2c_addresses = [0x20, 0x21]

			# Initialize MCP23008 devices and store them in a list
			self.mcp_devices = [MCP23008(bus, addr) for addr in i2c_addresses]
		except Exception as e:
			print(f"MCP23008 GPIO expanders not detected! — {type(e).__name__}: {e}")
			self.mcp_devices = None

	# Find MCP23008 device by I2C address
	def set_pin_from_address(self, i2c_address: int, pin: int, value: int) -> bool:
		"""Drive a pin. Returns True if the device was found and the write succeeded."""
		if self.mcp_devices is None:
			return False

		for mcp in self.mcp_devices:
			if mcp.address == i2c_address:
				return mcp.set_pin(pin, value)

		print(f"GPIO: no expander configured at 0x{i2c_address:02X} "
		      f"(pin {pin} = {value} dropped)")
		return False
