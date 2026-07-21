import json
import os
import sys
import time
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Any
from pydispatch import dispatcher
from midi import MIDI
from gpio import GPIO
from program_blue import ProgramBlue
from gamepad_input import USBGamepadReader, Button


@dataclass
class MovementStruct:
	description: str = ""  # A handy description of this movement

	key: str = ''  # A keyboard key press assigned to this movement
	key_mirror: Optional[str] = None  # Alternate key for mirroring (e.g., swapping left/right)
	gamepad_buttons: List[Button] = field(default_factory=list)  # Gamepad buttons assigned to this movement
	midi_note: int = 0  # A MIDI note assigned to this movement to be recorded in a sequencer
	program_blue_channel: int = -1  # The channel number assigned to this movement in Program Blue

	output_pin1: List[Any] = field(default_factory=list)  # Index 0: I2C address, index 1: pin number
	output_pin2: List[Any] = field(default_factory=list)  # Optional second IO pin array (usually the inverse of output_pin1)
	output_pin1_max_time: float = -1  # Maximum time (in seconds) for pin 1 to remain high (-1 means infinite)
	output_pin2_max_time: float = -1  # Maximum time for pin 2 (-1 means infinite)
	inverted: bool = False  # Invert high/low for this movement

	pin1_time: float = 0  # Timer for output_pin1
	pin2_time: float = 0  # Timer for output_pin2

	key_is_pressed: bool = False  # Tracks if the key is currently pressed


class Movement:
	all: List[MovementStruct] = []

	def __init__(self, config_path: str) -> None:
		self.b_mirrored: bool = False
		self.gpio = GPIO()
		self.midi = MIDI()
		self.program_blue = ProgramBlue()
		self.gamepad = USBGamepadReader()
		self.b_thread_started: bool = False

		self._load_movements(config_path)

		for movement in self.all:
			movement.key_is_pressed = False
			val = 1 if movement.inverted else 0
			movement.pin1_time = 0

			if movement.output_pin1:
				self.set_pin(movement.output_pin1, val, movement)
				if movement.output_pin2:
					movement.pin2_time = 0
					self.set_pin(movement.output_pin2, 1 - val, movement)

		dispatcher.connect(self.on_key_event, signal='keyEvent', sender=dispatcher.Any)
		dispatcher.connect(self.on_midi_event, signal='onMidiEvent', sender=dispatcher.Any)
		dispatcher.connect(self.on_gamepad_event, signal='gamepadEvent', sender=dispatcher.Any)
		dispatcher.connect(self.on_program_blue_event, signal='onProgramBlueEvent', sender=dispatcher.Any)

		dispatcher.connect(self.on_mirrored_mode_toggle, signal='mirrorModeToggle', sender=dispatcher.Any)
		dispatcher.connect(self.set_mirrored, signal='onMirroredMode', sender=dispatcher.Any)

	def _load_movements(self, config_path: str) -> None:
		with open(config_path, 'r') as f:
			config = json.load(f)

		for m in config.get('movements', []):
			movement = MovementStruct()
			movement.description        	= m['description']
			movement.key                	= m['key']
			movement.key_mirror         	= m.get('key_mirror')
			movement.gamepad_buttons    	= [Button[b] for b in m.get('gamepad_buttons', [])]
			movement.midi_note          	= m.get('midi_note', 0)
			movement.program_blue_channel 	= m.get('program_blue_channel', -1)
			movement.inverted           	= m.get('inverted', False)

			gpio = m.get('gpio', {})

			if 'pin1' in gpio:
				p = gpio['pin1']
				movement.output_pin1          = [int(p['address'], 16), p['pin']]
				movement.output_pin1_max_time = p.get('max_sec', -1)

			if 'pin2' in gpio:
				p = gpio['pin2']
				movement.output_pin2          = [int(p['address'], 16), p['pin']]
				movement.output_pin2_max_time = p.get('max_sec', -1)

			self.all.append(movement)

	def set_mirrored(self, val: bool) -> None:
		if self.b_mirrored == val:
			return
		self.b_mirrored = val
		print(f"Setting mirrored mode: {self.b_mirrored}")
		for movement in self.all:
			if movement.key_mirror:
				key_mirror = movement.key_mirror
				movement.key_mirror = movement.key
				movement.key = key_mirror

	def on_mirrored_mode_toggle(self) -> None:
		new_mirror_mode = not self.b_mirrored
		self.set_mirrored(new_mirror_mode)

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
				#print(movement.description)
				if val == 1 and not movement.key_is_pressed:
					movement.key_is_pressed = True
					b_do_callback = True
					dispatcher.send(signal="onMovementKeyActivated", key=movement.key, on=True)
				elif val == 0 and movement.key_is_pressed:
					movement.key_is_pressed = False
					b_do_callback = True
					dispatcher.send(signal="onMovementKeyActivated", key=movement.key, on=False)
				if b_do_callback:
					if not b_mute_output:
						self.midi.send_message(movement.midi_note, val)
						self.program_blue.send_channel(movement.program_blue_channel, val)
					if movement.inverted:
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

	def on_key_event(self, key: any, val: any) -> None:
		try:
			self.execute_movement(str(key).lower(), val)
		except Exception as e:
			print(f"Invalid key: {e}")

	def on_program_blue_event(self, channel: int, val: int) -> None:
		for movement in self.all:
			if movement.program_blue_channel == channel:
				self.execute_movement(movement.key, val, True)
				break

	def on_midi_event(self, midi_note: int, val: int) -> None:
		for movement in self.all:
			if movement.midi_note == midi_note:
				self.execute_movement(movement.key, val, True)
				break

	def on_gamepad_event(self, button: Button, val: int) -> None:
		for movement in self.all:
			if button in movement.gamepad_buttons:
				self.execute_movement(movement.key, val, True)
				break
