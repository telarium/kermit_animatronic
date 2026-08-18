#!/usr/bin/env python3
"""
animation_controller.py

Plays MIDI animation files as movement animations. The MIDI notes are the
same ones the character JSON assigns to each movement, so playback is just a
matter of dispatching "onMidiEvent" on the file's own schedule.

Only one animation is active at a time: starting a new one stops and replaces
whatever was playing, releasing any movements the old animation left held.

Signals handled (see set_dispatch_events):
	animationStart  — kwargs: name, bStartAtRandomTime, bLoop
	animationStop   — no kwargs
	animationPause  — no kwargs
	animationResume — no kwargs
"""

import os
import random
import threading
import time
from typing import List, Optional

from pydispatch import dispatcher

from midi import parse_file as parse_midi_file

MIDI_EXTENSIONS = ('.mid', '.midi')


class _Animation:
	"""One loaded MIDI animation plus the thread that plays it.

	Holds its own playhead so it can be paused and resumed without losing
	position, and tracks which notes it currently has ON so everything can be
	released cleanly on stop — a solenoid left energised is a stuck pose (or a
	burnt coil), so nothing may exit without releasing.
	"""

	TICK_S = 0.005  # scheduler resolution; well under the shortest MIDI gap

	def __init__(self, name: str, events: List[List], b_loop: bool = False, start_ms: float = 0.0) -> None:
		self.name = name
		self.events = events                 # [time_ms, midi_note, value], time-ordered
		self.b_loop = b_loop
		self.duration_ms = float(events[-1][0]) if events else 0.0

		self._position_ms = float(start_ms)
		self._active_notes = set()
		self._stop_event = threading.Event()
		self._running = threading.Event()    # set = playing, clear = paused
		self._running.set()
		self._thread: Optional[threading.Thread] = None

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

	def play(self) -> None:
		self._thread = threading.Thread(target=self._worker, daemon=True)
		self._thread.start()

	def pause(self) -> None:
		self._running.clear()

	def resume(self) -> None:
		self._running.set()

	def stop(self) -> None:
		"""Stop playback, wait for the thread, and release every held note."""
		self._stop_event.set()
		self._running.set()  # a paused thread has to wake up to see the stop
		if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
			self._thread.join(timeout=2.0)
		self._thread = None
		self.release_all()

	def is_playing(self) -> bool:
		return bool(self._thread and self._thread.is_alive())

	def release_all(self) -> None:
		"""Send note-off for everything this animation still has ON."""
		for note in sorted(self._active_notes):
			self._send(note, 0)

	# -------------------------------------------------------------------------
	# Internal
	# -------------------------------------------------------------------------

	def _send(self, note: int, val: int) -> None:
		if val:
			self._active_notes.add(note)
		else:
			self._active_notes.discard(note)
		dispatcher.send(signal="onMidiEvent", midi_note=note, val=val)

	def _seek(self, position_ms: float) -> int:
		"""Fast-forward to position_ms: fire whatever should already be ON at
		that point, and return the index of the first event still ahead."""
		state = {}
		index = 0
		while index < len(self.events) and self.events[index][0] <= position_ms:
			_t, note, val = self.events[index]
			state[note] = val
			index += 1
		for note, val in state.items():
			if val:
				self._send(note, 1)
		return index

	def _worker(self) -> None:
		index = self._seek(self._position_ms)
		origin = time.monotonic() - (self._position_ms / 1000.0)

		while not self._stop_event.is_set():
			if not self._running.is_set():
				# Paused: idle here, then rebase the clock on resume so the
				# playhead picks up where it stopped rather than jumping.
				self._running.wait(0.1)
				if self._stop_event.is_set():
					break
				if not self._running.is_set():
					continue
				origin = time.monotonic() - (self._position_ms / 1000.0)

			now_ms = (time.monotonic() - origin) * 1000.0
			self._position_ms = now_ms

			while index < len(self.events) and self.events[index][0] <= now_ms:
				_t, note, val = self.events[index]
				self._send(note, val)
				index += 1

			if index >= len(self.events) and now_ms >= self.duration_ms:
				if self.b_loop:
					self.release_all()
					index = 0
					self._position_ms = 0.0
					origin = time.monotonic()
					continue
				# One-shot: hold the final pose. Nothing is released here —
				# the notes stay ON until stop() or the next animation.
				self._position_ms = self.duration_ms
				print(f"AnimationController: '{self.name}' finished, holding final pose.")
				break

			time.sleep(self.TICK_S)


class AnimationController:
	def __init__(self, animation_dir: str) -> None:
		self.animation_dir = animation_dir
		self._current: Optional[_Animation] = None
		self._lock = threading.Lock()

		self.set_dispatch_events()
		print(f"AnimationController: initialized (animations from '{self.animation_dir}').")

	def set_dispatch_events(self) -> None:
		dispatcher.connect(self.on_animation_start, signal='animationStart', sender=dispatcher.Any)
		dispatcher.connect(self.on_animation_stop, signal='animationStop', sender=dispatcher.Any)
		dispatcher.connect(self.on_animation_pause, signal='animationPause', sender=dispatcher.Any)
		dispatcher.connect(self.on_animation_resume, signal='animationResume', sender=dispatcher.Any)

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

	def start(self, name: str, bStartAtRandomTime: bool = False, bLoop: bool = False) -> None:
		# Load and play a MIDI animation, replacing any current one.
		path = self._resolve_path(name)
		if path is None:
			return

		events = parse_midi_file(path)
		if not events:
			print(f"AnimationController: '{path}' has no usable events, nothing to play.")
			return

		duration_ms = float(events[-1][0])

		if not bLoop:
			# A one-shot animation should settle on its last frame, so drop the
			# note-offs sitting on the final timestamp. Otherwise the file's
			# own tail would immediately release the pose we want held.
			events = self._strip_trailing_note_offs(events)
			if not events:
				print(f"AnimationController: '{path}' is only note-offs, nothing to play.")
				return

		start_ms = random.uniform(0.0, duration_ms) if (bStartAtRandomTime and duration_ms > 0) else 0.0

		with self._lock:
			if self._current is not None:
				self._current.stop()
				self._current = None

			animation = _Animation(
				name=os.path.basename(path),
				events=events,
				b_loop=bLoop,
				start_ms=start_ms,
			)
			self._current = animation

		print(f"AnimationController: playing '{path}' ({len(events)} events, "
		      f"{duration_ms:.0f}ms, loop={bLoop}, start={start_ms:.0f}ms)")
		animation.play()

	def stop(self) -> None:
		"""Stop playback and drop the animation."""
		with self._lock:
			if self._current is None:
				return
			name = self._current.name
			self._current.stop()
			self._current = None
		print(f"AnimationController: stopped '{name}'.")

	def pause(self) -> None:
		"""Pause playback, keeping the animation loaded so it can resume."""
		with self._lock:
			if self._current is None:
				print("AnimationController: nothing to pause.")
				return
			self._current.pause()
			print(f"AnimationController: paused '{self._current.name}'.")

	def resume(self) -> None:
		"""Resume a paused animation from where it left off."""
		with self._lock:
			if self._current is None:
				print("AnimationController: nothing to resume.")
				return
			self._current.resume()
			print(f"AnimationController: resumed '{self._current.name}'.")

	# -------------------------------------------------------------------------
	# Dispatcher handlers
	# -------------------------------------------------------------------------

	def on_animation_start(self, name: str, bStartAtRandomTime: bool = False, bLoop: bool = False) -> None:
		self.start(name, bStartAtRandomTime, bLoop)

	def on_animation_stop(self) -> None:
		self.stop()

	def on_animation_pause(self) -> None:
		self.pause()

	def on_animation_resume(self) -> None:
		self.resume()

	# -------------------------------------------------------------------------
	# Internal
	# -------------------------------------------------------------------------

	def _resolve_path(self, name: str) -> Optional[str]:
		"""Add the .mid extension and the animation directory if either is
		missing, then confirm the file exists."""
		filename = str(name).strip()
		if not filename:
			print("AnimationController: no animation name given.")
			return None

		if not filename.lower().endswith(MIDI_EXTENSIONS):
			filename += '.mid'

		if os.path.isabs(filename):
			path = filename
		elif os.path.normpath(filename).startswith(os.path.normpath(self.animation_dir)):
			path = filename  # already prefixed by the caller
		else:
			path = os.path.join(self.animation_dir, filename)

		if not os.path.isfile(path):
			print(f"AnimationController: animation not found: '{path}'")
			return None
		return path

	@staticmethod
	def _strip_trailing_note_offs(events: List[List]) -> List[List]:
		"""Remove the note-off events that land on the final timestamp, so a
		one-shot animation holds its last frame instead of releasing it."""
		if not events:
			return events
		last_ms = events[-1][0]
		trimmed = [e for e in events if not (e[0] == last_ms and e[2] == 0)]
		return trimmed


if __name__ == "__main__":
	import sys

	directory = sys.argv[2] if len(sys.argv) > 2 else "animations/kermit/"
	animation_name = sys.argv[1] if len(sys.argv) > 1 else "wakeword"

	def on_midi(midi_note, val, **kwargs):
		print(f"  note={midi_note:3d} val={val}")

	dispatcher.connect(on_midi, signal="onMidiEvent")

	controller = AnimationController(animation_dir=directory)
	controller.start(animation_name, bStartAtRandomTime=False, bLoop=False)

	try:
		while True:
			time.sleep(0.1)
	except KeyboardInterrupt:
		controller.stop()
		print("Exiting.")
