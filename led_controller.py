#!/usr/bin/env python3
"""
led_controller.py

Drives the ReSpeaker XVF3800's LED ring to reflect what the voice pipeline is
doing:

	off        — idle, waiting on the wakeword
	listening  — DOA effect, one LED tracking the talker
	thinking   — breathing, while the LLM/TTS round trip and playback run

The breath is generated here rather than by the firmware. On firmware 2.0.6 the
built-in breath effect ignores LED_SPEED, rainbow does not rotate, and
LED_RING_COLOR mode does not light the ring at all. What does work is
single-colour mode, so the breath is produced by rewriting LED_COLOR every
frame from a worker thread. Firmware gamma correction is switched off for this,
since the curve is applied in software.

Configuration comes from the character JSON, e.g.

	"led": {
		"color":         "0x00FF00",
		"brightness":     200,
		"breath_period":  2.5,
		"breath_floor":   0.05,
		"fps":            20
	}

All device access happens on the worker thread, through the shared
respeaker.ReSpeaker handle rather than a private one — the DSP settings and the
LED ring are the same control endpoint. State changes are queued and coalesced,
so a caller on the audio path never blocks on USB.
"""

import json
import math
import queue
import threading
import time
from typing import Optional

from pydispatch import dispatcher

import respeaker

# LED_EFFECT values, from the XVF3800 control table.
EFFECT_OFF      = 0
EFFECT_BREATH   = 1
EFFECT_RAINBOW  = 2
EFFECT_SINGLE   = 3
EFFECT_DOA      = 4
EFFECT_RING     = 5

RING_LED_COUNT  = 12
GAMMA           = 2.2


class LEDController:
	STATE_OFF       = "off"
	STATE_LISTENING = "listening"
	STATE_THINKING  = "thinking"

	DEFAULT_COLOR         = 0x00FF00
	DEFAULT_BRIGHTNESS    = 200
	DEFAULT_BREATH_PERIOD = 2.5    # seconds for one full breath cycle
	DEFAULT_BREATH_FLOOR  = 0.05   # never fully dark, so the ring stays present
	DEFAULT_FPS           = 20

	def __init__(self, hardware_path: str, device=None) -> None:
		self.color         = self.DEFAULT_COLOR
		self.brightness    = self.DEFAULT_BRIGHTNESS
		self.breath_period = self.DEFAULT_BREATH_PERIOD
		self.breath_floor  = self.DEFAULT_BREATH_FLOOR
		self.fps           = self.DEFAULT_FPS
		self.apply_config(hardware_path)

		self._device = device if device is not None else respeaker.get_device()
		if not self._device.available:
			print("LEDController: no ReSpeaker control available, LED control disabled.")

		self._state = self.STATE_OFF
		self._anim_start = 0.0
		self._queue: queue.Queue = queue.Queue()
		self._stop_event = threading.Event()

		self._thread = threading.Thread(target=self._worker, daemon=True)
		self._thread.start()

		self.set_dispatch_events()
		print(f"LEDController: initialized (color=0x{self.color:06X}, "
		      f"breath={self.breath_period}s @ {self.fps}fps).")

	def set_dispatch_events(self) -> None:
		dispatcher.connect(self.on_led_state, signal='ledState', sender=dispatcher.Any)

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

	def apply_config(self, hardware_path: str) -> None:
		"""Read the optional 'led' block from the character JSON."""
		try:
			with open(hardware_path, 'r') as f:
				config = json.load(f)
		except Exception as e:
			print(f"LEDController: could not read '{hardware_path}': {e}")
			return

		led = config.get('led', {})
		self.color         = self._parse_color(led.get('color'), self.DEFAULT_COLOR)
		self.brightness    = max(0, min(255, int(led.get('brightness', self.DEFAULT_BRIGHTNESS))))
		self.breath_period = max(0.2, float(led.get('breath_period', self.DEFAULT_BREATH_PERIOD)))
		self.breath_floor  = max(0.0, min(1.0, float(led.get('breath_floor', self.DEFAULT_BREATH_FLOOR))))
		self.fps           = max(1, min(60, int(led.get('fps', self.DEFAULT_FPS))))

	def set_state(self, state: str) -> None:
		"""Queue a state change. Returns immediately — never touches USB on the
		caller's thread."""
		if state not in (self.STATE_OFF, self.STATE_LISTENING, self.STATE_THINKING):
			print(f"LEDController: unknown state '{state}', ignoring.")
			return
		self._queue.put(state)

	def off(self) -> None:
		self.set_state(self.STATE_OFF)

	def shutdown(self) -> None:
		"""Turn the ring off and stop the worker."""
		self._stop_event.set()
		self._queue.put(self.STATE_OFF)
		if self._thread.is_alive():
			self._thread.join(timeout=2.0)

	# -------------------------------------------------------------------------
	# Dispatcher handlers
	# -------------------------------------------------------------------------

	def on_led_state(self, state: str) -> None:
		self.set_state(state)

	# -------------------------------------------------------------------------
	# Internal
	# -------------------------------------------------------------------------

	@staticmethod
	def _parse_color(value, fallback: int) -> int:
		"""Accept 0xRRGGBB ints, "0x00FF00"/"#00FF00" strings, or [r, g, b]."""
		if value is None:
			return fallback
		try:
			if isinstance(value, int):
				return value & 0xFFFFFF
			if isinstance(value, (list, tuple)) and len(value) == 3:
				r, g, b = (int(c) & 0xFF for c in value)
				return (r << 16) | (g << 8) | b
			text = str(value).strip().lstrip('#')
			return int(text, 16) & 0xFFFFFF
		except Exception:
			print(f"LEDController: could not parse color '{value}', using default.")
			return fallback

	def _scaled_color(self, level: float) -> int:
		"""Apply a 0..1 level to the configured colour, gamma corrected."""
		level = max(0.0, min(1.0, level)) ** GAMMA
		scale = level * (self.brightness / 255.0)
		r = int(((self.color >> 16) & 0xFF) * scale)
		g = int(((self.color >> 8) & 0xFF) * scale)
		b = int((self.color & 0xFF) * scale)
		return (r << 16) | (g << 8) | b

	def _write(self, name: str, values: list) -> bool:
		"""Reconnection and USB failure handling belong to the shared device."""
		return self._device.write(name, values)

	def _enter_state(self, state: str) -> None:
		if state == self.STATE_OFF:
			self._write("LED_EFFECT", [EFFECT_OFF])

		elif state == self.STATE_LISTENING:
			self._write("LED_GAMMIFY", [0])
			self._write("LED_EFFECT", [EFFECT_SINGLE])
			self._write("LED_COLOR", [self._scaled_color(1.0)])

		elif state == self.STATE_THINKING:
			# Software-driven breath.
			self._write("LED_GAMMIFY", [0])
			self._write("LED_EFFECT", [EFFECT_SINGLE])
			self._anim_start = time.monotonic()
			self._animate_frame()

	def _animate_frame(self) -> None:
		"""One frame of the breath: a raised cosine over breath_period."""
		elapsed = time.monotonic() - self._anim_start
		phase = (1.0 - math.cos(2.0 * math.pi * elapsed / self.breath_period)) / 2.0
		level = self.breath_floor + (1.0 - self.breath_floor) * phase
		self._write("LED_COLOR", [self._scaled_color(level)])

	def _worker(self) -> None:
		while True:
			# Only the animated state needs a frame clock; the others block.
			timeout = (1.0 / self.fps) if self._state == self.STATE_THINKING else None
			try:
				state = self._queue.get(timeout=timeout)

				# Coalesce: if more states are queued, only the last matters.
				while True:
					try:
						state = self._queue.get_nowait()
					except queue.Empty:
						break

				if state != self._state:
					self._state = state
					self._enter_state(state)
			except queue.Empty:
				self._animate_frame()

			if self._stop_event.is_set() and self._queue.empty():
				self._write("LED_EFFECT", [EFFECT_OFF])
				return
