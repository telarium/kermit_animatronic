import os
import time
import pygame
import random
import threading
from enum import Enum, auto
from typing import List, Optional
from pydispatch import dispatcher
from midi import parse_file as parse_midi_file
from program_blue import parse_file as parse_shw_file

USB_SHOWS_DIR = "/mnt/usb/shows"

class ShowType(Enum):
	MIDI = auto()
	PROGRAM_BLUE = auto()


class ShowPlayer:
	def __init__(self, pygame_instance) -> None:
		self.pygame = pygame_instance

		self.show_list: List[str] = []
		self.active_show_name: Optional[str] = None
		self.paused: bool = False

		# Generic animation state — works for both MIDI and ProgramBlue.
		# Each entry: [time_ms, channel_or_note, value]
		self.anim_events: List[List] = []
		self.anim_states: dict = {}   # channel_or_note -> last dispatched value
		self.show_type: Optional[ShowType] = None

		self._play_thread: Optional[threading.Thread] = None
		self._stop_event = threading.Event()

		script_dir = os.path.dirname(os.path.abspath(__file__))
		self._local_show_dir = os.path.join(script_dir, "shows")

		self.get_show_list()

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

	def load_show(self, show_name: str) -> None:
		if show_name == "":
			if not self.show_list:
				print("ShowPlayer: no shows available for random selection.")
				return
			show_name = random.choice(self.show_list)

		# If this show is already loaded and paused, just unpause.
		if self.active_show_name == show_name and self.paused:
			self.toggle_pause()
			return

		# Stop whatever is currently playing.
		self._stop_playback()

		audio_path, events, show_type = self._resolve_show(show_name)
		if audio_path is None:
			print(f"ShowPlayer: could not find show '{show_name}'.")
			return

		self.active_show_name = show_name
		self.anim_events = events
		self.anim_states.clear()
		self.show_type = show_type

		self._stop_event.clear()
		self._play_thread = threading.Thread(
			target=self._play_worker,
			args=(audio_path,),
			daemon=True,
		)
		self._play_thread.start()

	def stop_show(self) -> None:
		self._stop_playback()
		self.active_show_name = None

	def toggle_pause(self) -> None:
		if not self.paused:
			self.paused = True
			self.pygame.mixer.music.pause()
		else:
			self.paused = False
			self.pygame.mixer.music.unpause()

	def get_show_list(self) -> None:
		"""Scan all known show directories and build the combined show list."""
		audio_extensions = ('.mp3', '.wav', '.ogg')
		found: List[str] = []

		dirs_to_scan = [USB_SHOWS_DIR, self._local_show_dir]
		for directory in dirs_to_scan:
			if not os.path.isdir(directory):
				continue
			files = os.listdir(directory)
			files_lower = {f.lower() for f in files}
			for f in files:
				# .shw files are self-contained — no sidecar needed.
				if f.lower().endswith('.shw'):
					base = os.path.splitext(f)[0]
					if base not in found:
						found.append(base)
				elif f.lower().endswith(audio_extensions):
					base = os.path.splitext(f)[0]
					if (base + '.mid') in files_lower and base not in found:
						found.append(base)

		self.show_list = found

		if found:
			dispatcher.send(signal="showListLoad", show_list=found)
		else:
			print("ShowPlayer: no shows found in any show directory.")

	# -------------------------------------------------------------------------
	# Internal: show resolution
	# -------------------------------------------------------------------------

	def _resolve_show(self, show_name: str):
		"""
		Search for the show in all known locations.
		Returns (audio_path, events, ShowType) or (None, [], None) on failure.
		"""
		audio_extensions = ['.mp3', '.wav', '.ogg']
		dirs_to_search = [USB_SHOWS_DIR, self._local_show_dir]

		for directory in dirs_to_search:
			if not os.path.isdir(directory):
				continue

			# Try ProgramBlue first.
			shw_path = os.path.join(directory, show_name + '.shw')
			if os.path.isfile(shw_path):
				print(f"ShowPlayer: loading ProgramBlue show: {shw_path}")
				audio_path, events = parse_shw_file(shw_path)
				return audio_path, events, ShowType.PROGRAM_BLUE

			# Try audio + MIDI pair.
			for ext in audio_extensions:
				audio_path = os.path.join(directory, show_name + ext)
				midi_path  = os.path.join(directory, show_name + '.mid')
				if os.path.isfile(audio_path) and os.path.isfile(midi_path):
					print(f"ShowPlayer: loading MIDI show: {audio_path} + {midi_path}")
					events = parse_midi_file(midi_path)
					return audio_path, events, ShowType.MIDI

		return None, [], None

	# -------------------------------------------------------------------------
	# Internal: playback thread
	# -------------------------------------------------------------------------

	def _play_worker(self, audio_path: str) -> None:
		try:
			self.pygame.mixer.music.load(audio_path)
			self.pygame.mixer.music.play()
			print(f"ShowPlayer: playing '{audio_path}'")

			while not self._stop_event.is_set():
				if not self.pygame.mixer.music.get_busy() and not self.paused:
					# Playback finished naturally.
					break

				if not self.paused:
					current_ms = self.pygame.mixer.music.get_pos()
					self._dispatch_events(current_ms)

				time.sleep(0.01)

		except Exception as e:
			print(f"ShowPlayer: error during playback: {e}")
		finally:
			self.pygame.mixer.music.stop()
			self.paused = False
			if not self._stop_event.is_set():
				# Natural end — notify the rest of the system.
				dispatcher.send(signal="showEnd")
			self.active_show_name = None

	def _dispatch_events(self, current_ms: int) -> None:
		"""Fire animation events whose timestamp has been reached, removing them as we go."""
		pending = []
		for entry in self.anim_events:
			event_ms, key, value = entry
			if event_ms <= current_ms:
				if self.anim_states.get(key) != value:
					self.anim_states[key] = value
					self._dispatch_single(key, value)
			else:
				pending.append(entry)
		self.anim_events = pending

	def _dispatch_single(self, key: int, value: int) -> None:
		if self.show_type == ShowType.MIDI:
			print(f"ShowPlayer: MIDI note={key} val={value}")
			dispatcher.send(signal="onMidiEvent", midi_note=key, val=value)
		elif self.show_type == ShowType.PROGRAM_BLUE:
			print(f"ShowPlayer: PB channel={key} val={value}")
			dispatcher.send(signal="onProgramBlueEvent", channel=key, val=value)

	def _stop_playback(self) -> None:
		"""Signal the play thread to stop and wait for it."""
		self._stop_event.set()
		self.pygame.mixer.music.stop()
		if self._play_thread and self._play_thread.is_alive():
			self._play_thread.join(timeout=2)
		self._play_thread = None
		self.paused = False