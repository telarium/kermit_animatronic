import threading
import time
from evdev import InputDevice, ecodes, list_devices, InputEvent
from enum import Enum
from dataclasses import dataclass
from pydispatch import dispatcher
from typing import Optional, Dict, Any, List

class Button(Enum):
	# Bumpers
	LEFT_BUMPER  = 0
	RIGHT_BUMPER = 1

	# Left Stick
	LEFT_STICK_LEFT  = 2
	LEFT_STICK_RIGHT = 3
	LEFT_STICK_UP    = 4
	LEFT_STICK_DOWN  = 5

	# Right Stick
	RIGHT_STICK_LEFT  = 6
	RIGHT_STICK_RIGHT = 7
	RIGHT_STICK_UP    = 8
	RIGHT_STICK_DOWN  = 9

	# Triggers
	LEFT_TRIGGER  = 10
	RIGHT_TRIGGER = 11

	# Face Buttons
	BTN_SOUTH = 12
	BTN_EAST  = 13
	BTN_NORTH = 14
	BTN_WEST  = 15

	# Hat (analog stick button)
	BTN_THUMBL = 16
	BTN_THUMBR = 17

	# D-pad
	DPAD_LEFT  = 18
	DPAD_RIGHT = 19
	DPAD_DOWN  = 20
	DPAD_UP    = 21

	# Start and Select
	START  = 22
	SELECT = 23

class Direction(Enum):
	NEUTRAL    = 'neutral'
	UP         = 'up'
	DOWN       = 'down'
	LEFT       = 'left'
	RIGHT      = 'right'
	UP_LEFT    = 'up-left'
	UP_RIGHT   = 'up-right'
	DOWN_LEFT  = 'down-left'
	DOWN_RIGHT = 'down-right'

@dataclass
class StickState:
	direction: Direction = Direction.NEUTRAL
	x: int = 0
	y: int = 0

class USBGamepadReader:
	TRIGGER_THRESHOLD = 128  # Half of 255

	KEY_BUTTON_MAP: Dict[int, Button] = {
		ecodes.BTN_TL: Button.LEFT_BUMPER,
		ecodes.BTN_TR: Button.RIGHT_BUMPER,
		304:           Button.BTN_SOUTH,
		305:           Button.BTN_EAST,
		307:           Button.BTN_WEST,
		308:           Button.BTN_NORTH,
		317:           Button.BTN_THUMBL,
		318:           Button.BTN_THUMBR,
		314:           Button.SELECT,
		315:           Button.START,
	}

	def __init__(self) -> None:
		self.start_button_down: bool = False
		self.select_button_down: bool = False
		self._trigger_states: Dict[Button, bool] = {}

		self.device: Optional[InputDevice] = self._find_gamepad()
		if self.device:
			print(f"Gamepad detected: {self.device.name} ({self.device.path})")
			self.left_stick: StickState = StickState()
			self.right_stick: StickState = StickState()
			self.dpad_states: Dict[str, bool] = {'left': False, 'right': False, 'up': False, 'down': False}
			self.abs_ranges: Dict[int, Dict[str, int]] = self._get_abs_ranges()
			self.update_thread: threading.Thread = threading.Thread(target=self.read_inputs, daemon=True)
			self.update_thread.start()
		else:
			print("No gamepad detected.")

	def _find_gamepad(self) -> Optional[InputDevice]:
		devices = [InputDevice(path) for path in list_devices()]
		for device in devices:
			if 'hdmi' in device.name.lower():
				continue
			if any(keyword in device.name.lower() for keyword in ['gamepad', 'joystick', 'controller']):
				return device
			capabilities = device.capabilities()
			keys = capabilities.get(ecodes.EV_KEY, [])
			axes = capabilities.get(ecodes.EV_ABS, [])
			if ecodes.ABS_X in axes and (
				ecodes.BTN_GAMEPAD in keys or
				ecodes.BTN_JOYSTICK in keys
			):
				return device
		return None

	def _get_abs_ranges(self) -> Dict[int, Dict[str, int]]:
		abs_info = self.device.capabilities().get(ecodes.EV_ABS, []) if self.device else []
		ranges: Dict[int, Dict[str, int]] = {}
		for code, info in abs_info:
			if isinstance(info, tuple) and len(info) >= 6:
				min_val, max_val = info[1], info[2]
				ranges[code] = {'min': min_val, 'max': max_val}
		return ranges

	def read_inputs(self) -> None:
		while True:
			if not self.device:
				print("No gamepad device available. Trying to reconnect...")
				self.device = self._find_gamepad()
				if self.device:
					print(f"Reconnected to {self.device.name} ({self.device.path})")
					self.abs_ranges = self._get_abs_ranges()
				else:
					time.sleep(2)
					continue

			print(f"Listening for inputs on {self.device.name}...")
			try:
				for event in self.device.read_loop():
					if event.type == ecodes.EV_KEY:
						self._process_button_event(event)
					elif event.type == ecodes.EV_ABS:
						self._process_abs_event(event)
			except OSError as e:
				print(f"Device error: {e}. Attempting to reconnect...")
				self.device = None
				time.sleep(1)

	def _dispatch(self, button: Button, val: int) -> None:
		print(f"Gamepad: button={button.name} val={val}")
		dispatcher.send(signal="gamepadEvent", button=button, val=val)

	def _process_button_event(self, event: InputEvent) -> None:
		keycode = event.code
		if keycode in self.KEY_BUTTON_MAP:
			button = self.KEY_BUTTON_MAP[keycode]
			self._dispatch(button, event.value)

			if button == Button.START:
				self.start_button_down = bool(event.value)
			elif button == Button.SELECT:
				self.select_button_down = bool(event.value)

			if self.start_button_down and self.select_button_down:
				dispatcher.send(signal="mirrorModeToggle")
		else:
			print(f"Gamepad: unmapped button code={keycode} val={event.value}")

	def _process_abs_event(self, event: InputEvent) -> None:
		code = event.code
		value = event.value

		if code == ecodes.ABS_HAT0X:
			self._handle_dpad('left', 'right', value)
		elif code == ecodes.ABS_HAT0Y:
			self._handle_dpad('up', 'down', value)
		elif code == ecodes.ABS_Z:
			self._handle_trigger(Button.LEFT_TRIGGER, value)
		elif code == ecodes.ABS_RZ:
			self._handle_trigger(Button.RIGHT_TRIGGER, value)
		else:
			self._handle_stick(event)

	def _handle_trigger(self, button: Button, value: int) -> None:
		pressed = value > self.TRIGGER_THRESHOLD
		current = self._trigger_states.get(button, False)
		if pressed != current:
			self._trigger_states[button] = pressed
			self._dispatch(button, int(pressed))

	def _handle_dpad(self, negative_dir: str, positive_dir: str, value: int) -> None:
		if value == -1:
			self._set_dpad_state(negative_dir, True)
			self._set_dpad_state(positive_dir, False)
		elif value == 1:
			self._set_dpad_state(negative_dir, False)
			self._set_dpad_state(positive_dir, True)
		else:
			self._set_dpad_state(negative_dir, False)
			self._set_dpad_state(positive_dir, False)

	def _set_dpad_state(self, direction: str, pressed: bool) -> None:
		button = getattr(Button, f"DPAD_{direction.upper()}", None)
		if button and self.dpad_states[direction] != pressed:
			self.dpad_states[direction] = pressed
			self._dispatch(button, int(pressed))

	def _handle_stick(self, event: InputEvent) -> None:
		axis_map = {
			ecodes.ABS_X:  ('left', 'x'),
			ecodes.ABS_Y:  ('left', 'y'),
			ecodes.ABS_RX: ('right', 'x'),
			ecodes.ABS_RY: ('right', 'y')
		}
		if event.code in axis_map:
			stick, axis = axis_map[event.code]
			stick_state: StickState = getattr(self, f"{stick}_stick")
			setattr(stick_state, axis, event.value)
			direction = self._get_direction(stick, stick_state.x, stick_state.y)
			if direction != stick_state.direction:
				self._update_stick_direction(stick_state, direction, stick)

	def _update_stick_direction(self, stick_state: StickState, new_direction: Direction, stick: str) -> None:
		self._change_stick_keys(stick_state.direction, stick, pressed=False)
		stick_state.direction = new_direction
		self._change_stick_keys(new_direction, stick, pressed=True)

	def _change_stick_keys(self, direction: Direction, stick: str, pressed: bool) -> None:
		if direction != Direction.NEUTRAL:
			keys: List[Button] = self._direction_to_keys(direction, stick)
			for key in keys:
				self._dispatch(key, int(pressed))

	def _direction_to_keys(self, direction: Direction, stick: str) -> List[Button]:
		directions = direction.value.split('-')
		keys: List[Button] = []
		for dir in directions:
			button_name = f"{stick.upper()}_STICK_{dir.upper()}"
			button = getattr(Button, button_name, None)
			if button:
				keys.append(button)
		return keys

	def _get_direction(self, stick: str, x: int, y: int) -> Direction:
		axis_x = ecodes.ABS_X if stick == 'left' else ecodes.ABS_RX
		axis_y = ecodes.ABS_Y if stick == 'left' else ecodes.ABS_RY

		min_x, max_x = self.abs_ranges[axis_x]['min'], self.abs_ranges[axis_x]['max']
		min_y, max_y = self.abs_ranges[axis_y]['min'], self.abs_ranges[axis_y]['max']

		norm_x = (2 * (x - min_x) / (max_x - min_x)) - 1
		norm_y = (2 * (y - min_y) / (max_y - min_y)) - 1

		DEAD_ZONE = 0.2
		norm_x = norm_x if abs(norm_x) > DEAD_ZONE else 0
		norm_y = norm_y if abs(norm_y) > DEAD_ZONE else 0

		direction_parts = []
		if norm_y < -DEAD_ZONE:
			direction_parts.append('up')
		elif norm_y > DEAD_ZONE:
			direction_parts.append('down')
		if norm_x < -DEAD_ZONE:
			direction_parts.append('left')
		elif norm_x > DEAD_ZONE:
			direction_parts.append('right')

		if direction_parts:
			return Direction('-'.join(direction_parts))
		else:
			return Direction.NEUTRAL
