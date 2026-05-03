from dataclasses import dataclass, field
from typing import List, Optional, Callable, Any
from pydispatch import dispatcher
from midi import MIDI
from program_blue import ProgramBlue
import time
import threading
import random

@dataclass
class MovementStruct:
	key: str = ''  # A keyboard key press assigned to this movement
	description: str = ""  # A handy description of this movement
	output_pin1: List[Any] = field(default_factory=list)  # Index 0: I2C address, index 1: pin number
	output_pin2: List[Any] = field(default_factory=list)  # Optional second IO pin array (usually the inverse of output_pin1)
	midi_note: int = 0  # A MIDI note assigned to this movement to be recorded in a sequencer
	program_blue_channel: int = -1 # The channel number assigned to this movement in Program Blue
	output_pin1_max_time: float = -1  # Maximum time (in seconds) for pin 1 to remain high (-1 means infinite)
	output_pin2_max_time: float = -1  # Maximum time for pin 2 (-1 means infinite)
	output_inverted: bool = False  # Invert high/low for this movement
	mirrored_key: Optional[str] = None  # Alternate key for mirroring (e.g., swapping left/right)
	key_is_pressed: bool = False  # Tracks if the key is currently pressed
	pin1_time: float = 0  # Timer for output_pin1
	pin2_time: float = 0  # Timer for output_pin2

class Movement:
	all: List[MovementStruct] = []

	def __init__(self, gpio: Any) -> None:
		self.b_mirrored: bool = False  # Swap left/right body movement to mirror animation
		self.gpio = gpio
		self.midi = MIDI()
		self.program_blue = ProgramBlue()
		self.b_thread_started: bool = False

		# Define movements
		self.mouth = MovementStruct()
		self.mouth.description = "Mouth"
		self.mouth.key = 'x'
		self.mouth.output_pin1 = [0x20, 7]  # Mouth open
		self.mouth.output_pin2 = [0x23, 2]  # Mouth close
		self.mouth.output_pin1_max_time = 0.75
		self.mouth.midi_note = 56
		self.mouth.program_blue_channel = 0
		self.all.append(self.mouth)

		self.head_turn_left = MovementStruct()
		self.head_turn_left.description = "Head Turn L"
		self.head_turn_left.key = 'a'
		self.head_turn_left.output_pin1 = [0x21, 0]
		self.head_turn_left.output_pin1_max_time = 1
		self.head_turn_left.midi_note = 61
		self.head_turn_left.program_blue_channel = 1
		self.head_turn_left.mirrored_key = 'd'
		self.all.append(self.head_turn_left)

		self.head_right = MovementStruct()
		self.head_right.description = "Head Turn R"
		self.head_right.key = 'd'
		self.head_right.output_pin1 = [0x23, 3]
		self.head_right.output_pin1_max_time = 1
		self.head_right.midi_note = 62
		self.head_right.program_blue_channel = 2
		self.head_right.mirrored_key = 'a'
		self.all.append(self.head_right)

		self.head_tilt_up = MovementStruct()
		self.head_tilt_up.description = "Head Tilt Up"
		self.head_tilt_up.key = 's'
		self.head_tilt_up.output_pin1 = [0x20, 3]  # Head tilt down
		self.head_tilt_up.output_pin2 = [0x21, 6]  # Head tilt up
		self.head_tilt_up.midi_note = 63
		self.head_tilt_up.program_blue_channel = 3
		self.all.append(self.head_tilt_up)

		self.head_tilt_left = MovementStruct()
		self.head_tilt_left.description = "Head Tilt L"
		self.head_tilt_left.key = 'q'
		self.head_tilt_left.output_pin1 = [0x21, 0]
		self.head_tilt_left.output_pin1_max_time = 1
		self.head_tilt_left.midi_note = 61
		self.head_tilt_left.program_blue_channel = 4
		self.head_tilt_left.mirrored_key = 'e'
		self.all.append(self.head_tilt_left)

		self.head_tilt_right = MovementStruct()
		self.head_tilt_right.description = "Head Tilt R"
		self.head_tilt_right.key = 'e'
		self.head_tilt_right.output_pin1 = [0x21, 0]
		self.head_tilt_right.output_pin1_max_time = 1
		self.head_tilt_right.midi_note = 61
		self.head_tilt_right.program_blue_channel = 5
		self.head_tilt_right.mirrored_key = 'q'
		self.all.append(self.head_tilt_right)

		self.body_lean_up = MovementStruct()
		self.body_lean_up.description = "Body Lean Up"
		self.body_lean_up.key = 'w'
		self.body_lean_up.output_pin1 = [0x23, 4]  # Lean up
		self.body_lean_up.output_pin2 = [0x21, 1]  # Lean down
		self.body_lean_up.midi_note = 64
		self.body_lean_up.program_blue_channel = 6
		self.all.append(self.body_lean_up)

		self.body_turn_left = MovementStruct()
		self.body_turn_left.description = "Body Turn Left"
		self.body_turn_left.key = 'z'
		self.body_turn_left.output_pin1 = [0x23, 4]  # Body turn L
		self.body_turn_left.midi_note = 64
		self.body_turn_left.program_blue_channel = 7
		self.body_turn_left.mirrored_key = 'c'
		self.all.append(self.body_turn_left)

		self.body_turn_right = MovementStruct()
		self.body_turn_right.description = "Body Turn Right"
		self.body_turn_right.key = 'c'
		self.body_turn_right.output_pin1 = [0x23, 4]  # Body turn R
		self.body_turn_right.midi_note = 64
		self.body_turn_right.program_blue_channel = 8
		self.body_turn_right.mirrored_key = 'z'
		self.all.append(self.body_turn_right)

		self.animation_threads_active: bool = False
		self.head_nod_animation_thread: Optional[threading.Thread] = None  # Head nod animation thread
		self.neck_animation_thread: Optional[threading.Thread] = None  # Head (neck) animation thread

		for movement in self.all:
			movement.key_is_pressed = False
			val = 0
			try:
				if movement.output_inverted:
					val = 1
			except Exception:
				movement.output_inverted = False

			movement.pin1_time = 0

			if movement.output_pin1:
				self.set_pin(movement.output_pin1, val, movement)
				if movement.output_pin2:
					movement.pin2_time = 0
					self.set_pin(movement.output_pin2, 1 - val, movement)

	def set_mirrored(self, b_mirrored: bool) -> None:
		if self.b_mirrored == b_mirrored:
			return
		self.b_mirrored = b_mirrored
		print(f"Setting mirrored mode: {self.b_mirrored}")
		for movement in self.all:
			if movement.mirrored_key:
				mirrored_key = movement.mirrored_key
				movement.mirrored_key = movement.key
				movement.key = mirrored_key

	def get_all_movement_info(self) -> List[List[Any]]:
		all_movements = []
		for movement in self.all:
			all_movements.append([movement.key, movement.midi_note])
		return all_movements

	def update_pins(self) -> None:
		while True:
			time.sleep(0.1)
			for movement in self.all:
				if movement.output_pin1_max_time > -1 and movement.pin1_time > 0:
					movement.pin1_time -= 0.1
					if movement.pin1_time <= 0:
						movement.pin1_time = 0
						self.set_pin(movement.output_pin1, 0, movement)
				if movement.output_pin2_max_time > -1 and movement.pin2_time > 0:
					movement.pin2_time -= 0.1
					if movement.pin2_time <= 0:
						movement.pin2_time = 0
						self.set_pin(movement.output_pin2, 0, movement)

	def set_pin(self, pin: List[Any], val: int, movement: MovementStruct) -> None:
		self.gpio.set_pin_from_address(pin[0], pin[1], val)

	def execute_movement(self, key: str, val: int, b_mute_output: bool = False) -> bool:
		b_do_callback = False
		for movement in self.all:
			if movement.key == key and key:
				if val == 1 and not movement.key_is_pressed:
					movement.key_is_pressed = True
					b_do_callback = True
				elif val == 0 and movement.key_is_pressed:
					movement.key_is_pressed = False
					b_do_callback = True
				if b_do_callback:
					if not b_mute_output:
						# Output movement data over MIDI and ProgramBlue
						self.midi.send_message(movement.midi_note, val)
						self.program_blue.send_channel(movement.program_blue_channel, val) #TODO, this may not work! If we, we'll remove it.
					if movement.output_inverted:
						val = 1 - val
					self.set_pin(movement.output_pin1, val, movement)
					movement.pin1_time = movement.output_pin1_max_time if val == 1 else 0
					if movement.output_pin2:
						self.set_pin(movement.output_pin2, 1 - val, movement)
						movement.pin2_time = 0 if val == 1 else movement.output_pin2_max_time
					break
		if not self.b_thread_started:
			self.b_thread_started = True
			t = threading.Thread(target=self.update_pins, daemon=True)
			t.start()
		return b_do_callback

	def execute_program_blue_channel(self, channel: int, val: int) -> None:
		for movement in self.all:
			if movement.program_blue_channel == channel:
				self.execute_movement(movement.key, val, True)
				break

	def execute_midi_note(self, midi_note: int, val: int) -> None:
		for movement in self.all:
			if movement.midi_note == midi_note:
				self.execute_movement(movement.key, val, True)
				break