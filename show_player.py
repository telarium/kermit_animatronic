import bisect
import collections
import gzip
import json
import os
import time
import pygame
import random
import requests
import threading
import uuid
import audio_setup
from enum import Enum, auto
from typing import Callable, Dict, List, Optional, Tuple
from pydispatch import dispatcher
from midi import parse_file as parse_midi_file
from program_blue import parse_file as parse_shw_file
from logger import get_logger

log = get_logger(__name__)



class ShowType(Enum):
	MIDI = auto()
	PROGRAM_BLUE = auto()


class ShowPlayer:
	# Music playback level, 0.0-1.0.
	SHOW_VOLUME = 0.8

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
		# The complete event list of the loaded show. anim_events is consumed
		# as playback proceeds; this copy is what a group show sends to peers.
		self.show_events: List[List] = []

		# Playhead as last read from the mixer, and whether the audio has
		# actually started (the DAC wake runs before play()).
		self._position_ms: float = 0.0
		self._started: bool = False

		# Played when a play request is refused because this character is
		# performing in another character's show.
		self._busy_sound_path: str = ""

		self._play_thread: Optional[threading.Thread] = None
		self._stop_event = threading.Event()

		# Whichever directory the storage layer has made current — the USB
		# drive's shows when it is the source of truth, the local backup
		# otherwise. Set by start.py on every config load.
		script_dir = os.path.dirname(os.path.abspath(__file__))
		self.show_dir = os.path.join(script_dir, "shows")

		self.get_show_list()

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

	def load_show(self, show_name: str) -> None:
		if show_name == "":
			if not self.show_list:
				log.warning("ShowPlayer: no shows available for random selection.")
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
			log.error(f"ShowPlayer: could not find show '{show_name}'.")
			return

		self.active_show_name = show_name
		self.anim_events = events
		self.show_events = [list(e) for e in events]
		self.anim_states.clear()
		self.show_type = show_type
		self._position_ms = 0.0
		self._started = False

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

	def set_busy_sound(self, path: str) -> None:
		self._busy_sound_path = path

	def play_busy_sound(self) -> None:
		"""A short "no" sound effect. Played through mixer.Sound, never
		mixer.music: a busy host is using that single channel for its song,
		and VoicePlayer would puppeteer and re-enable the wakeword."""
		path = self._busy_sound_path
		if not path:
			log.info("Busy sound: no busy_sound in the character JSON.")
			return
		if not os.path.isfile(path):
			log.warning(f"Busy sound: '{path}' not found.")
			return

		def play():
			try:
				audio_setup.wake_dac_if_needed(self.pygame)
				sound = self.pygame.mixer.Sound(path)
				sound.play()
				time.sleep(sound.get_length())
			except Exception as e:
				log.exception(f"Busy sound: playback failed: {e}")
			finally:
				audio_setup.note_playback()
		threading.Thread(target=play, daemon=True).start()

	def get_state(self) -> tuple:
		"""(position_ms, playing) of the current show, for group-show
		heartbeats. Reports paused at 0 until the audio has actually started,
		and holds the last position while paused."""
		playing = bool(self.active_show_name) and self._started and not self.paused
		return self._position_ms, playing

	def is_active(self) -> bool:
		return self.active_show_name is not None

	def show_type_name(self) -> str:
		return SHOW_TYPE_PROGRAM_BLUE if self.show_type == ShowType.PROGRAM_BLUE else SHOW_TYPE_MIDI

	def set_show_directory(self, path: str) -> None:
		"""Switch to a different show directory and rescan it."""
		if not path or os.path.abspath(path) == os.path.abspath(self.show_dir):
			return
		self.show_dir = path
		log.info(f"ShowPlayer: show directory is now '{self.show_dir}'")
		self.get_show_list()

	def get_show_list(self) -> None:
		"""Scan the active show directory and build the show list."""
		audio_extensions = ('.mp3', '.wav', '.ogg')
		found: List[str] = []

		if os.path.isdir(self.show_dir):
			files = os.listdir(self.show_dir)
			files_lower = {f.lower() for f in files}
			for f in files:
				# .shw files are self-contained — no sidecar needed.
				if f.lower().endswith('.shw'):
					base = os.path.splitext(f)[0]
					if base not in found:
						found.append(base)
				elif f.lower().endswith(audio_extensions):
					base = os.path.splitext(f)[0]
					if (base.lower() + '.mid') in files_lower and base not in found:
						found.append(base)

		self.show_list = found

		if not found:
			log.warning(f"ShowPlayer: no shows found in '{self.show_dir}'.")
		# Sent either way, so switching to an empty directory clears the UI.
		dispatcher.send(signal="showListLoad", show_list=found)

	# -------------------------------------------------------------------------
	# Internal: show resolution
	# -------------------------------------------------------------------------

	def _resolve_show(self, show_name: str):
		"""
		Search for the show in all known locations.
		Returns (audio_path, events, ShowType) or (None, [], None) on failure.
		"""
		audio_extensions = ['.mp3', '.wav', '.ogg']
		directory = self.show_dir

		if os.path.isdir(directory):
			# Try ProgramBlue first.
			shw_path = os.path.join(directory, show_name + '.shw')
			if os.path.isfile(shw_path):
				audio_path, events = parse_shw_file(shw_path)
				channels = sorted(set(e[1] for e in events))
				duration = max((e[0] for e in events), default=0)
				log.info(f"ShowPlayer: {len(events)} events on channels {channels}, "
				      f"last at {duration}ms")
				return audio_path, events, ShowType.PROGRAM_BLUE

			# Try audio + MIDI pair.
			for ext in audio_extensions:
				audio_path = os.path.join(directory, show_name + ext)
				midi_path  = os.path.join(directory, show_name + '.mid')
				if os.path.isfile(audio_path) and os.path.isfile(midi_path):
					log.info(f"ShowPlayer: loading MIDI show: {audio_path} + {midi_path}")
					events = parse_midi_file(midi_path)
					return audio_path, events, ShowType.MIDI

		return None, [], None

	# -------------------------------------------------------------------------
	# Internal: playback thread
	# -------------------------------------------------------------------------

	def _play_worker(self, audio_path: str) -> None:
		try:
			# Bring the DAC up before loading, or the opening moment of the
			# show gets swallowed. Shared with VoicePlayer, so this is a no-op
			# when Kermit has just finished speaking.
			audio_setup.wake_dac_if_needed(self.pygame)

			self.pygame.mixer.music.load(audio_path)
			# mixer.music is a single global channel shared with VoicePlayer,
			# and its volume persists across load(). Set it every time rather
			# than once at init, or whichever component played last decides
			# the level for the next one.
			self.pygame.mixer.music.set_volume(self.SHOW_VOLUME)
			self.pygame.mixer.music.play()
			self._started = True
			log.info(f"ShowPlayer: playing '{audio_path}' at {self.SHOW_VOLUME:.0%} volume")

			while not self._stop_event.is_set():
				if not self.pygame.mixer.music.get_busy() and not self.paused:
					# Playback finished naturally.
					break

				if not self.paused:
					current_ms = self.pygame.mixer.music.get_pos()
					if current_ms >= 0:
						self._position_ms = float(current_ms)
					self._dispatch_events(current_ms)

				time.sleep(0.01)

		except Exception as e:
			log.exception(f"ShowPlayer: error during playback: {e}")
		finally:
			self.pygame.mixer.music.stop()
			self.paused = False
			self._started = False
			# Stamp the END of the show, not the start
			audio_setup.note_playback()
			if not self._stop_event.is_set():
				# Natural end — notify the rest of the system.
				dispatcher.send(signal="showStatus", status="end")
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
			#print(f"ShowPlayer: MIDI note={key} val={value}")
			dispatcher.send(signal="onMidiEvent", midi_note=key, val=value)
		elif self.show_type == ShowType.PROGRAM_BLUE:
			#print(f"ShowPlayer: PB channel={key} val={value}")
			dispatcher.send(signal="onProgramBlueEvent", channel=key, val=value)

	def _stop_playback(self) -> None:
		"""Signal the play thread to stop and wait for it."""
		self._stop_event.set()
		self.pygame.mixer.music.stop()
		if self._play_thread and self._play_thread.is_alive():
			self._play_thread.join(timeout=2)
		self._play_thread = None
		self.paused = False


# =============================================================================
# ClockedTimeline — plays show events against a clock someone else owns
# =============================================================================
#
# A peer in a group show has no audio of its own to time against. It follows
# the host's playhead instead, which arrives as occasional references: "the
# show was at position P, playing (or paused), at this moment". Between
# references the timeline runs on the local monotonic clock; when a reference
# shows the local playhead has drifted past CORRECTION_THRESHOLD_MS, it jumps.
#
# Jumps go through seek(), which computes what every key should be at the new
# position and dispatches only the differences. That is what lets a character
# join partway through a song, or correct backwards, and still end up holding
# exactly the right pose.
#
# Events are [time_ms, key, value]. Keys and values are opaque here — today a
# MIDI note or ProgramBlue channel with 0/1, later possibly servo positions — so
# this class never needs to know which.


class ClockedTimeline:
	TICK_S = 0.005
	# Beyond this the local playhead is re-based onto the host's. Below it,
	# network jitter would cause constant tiny jumps for no visible benefit.
	CORRECTION_THRESHOLD_MS = 40.0

	def __init__(
		self,
		events: List[List],
		dispatch: Callable[[object, int], None],
		sync_offset_ms: float = 0.0,
		label: str = "",
	) -> None:
		self.events = sorted(events, key=lambda e: e[0])
		self._times = [e[0] for e in self.events]
		self.dispatch = dispatch
		# Positive = this character performs later than the host's playhead.
		self.sync_offset_ms = float(sync_offset_ms)
		self.label = label

		self._lock = threading.RLock()
		self._state: Dict[object, int] = {}
		self._index = 0
		self._started = False
		self._playing = False
		self._origin = 0.0			# local monotonic time at position 0
		self._frozen_ms = 0.0		# position while paused

		self._stop_event = threading.Event()
		self._thread: Optional[threading.Thread] = None

		self.corrections = 0

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

	def update_reference(self, position_ms: float, reference_local: float, playing: bool) -> None:
		"""The host's playhead was at position_ms at local monotonic time
		reference_local (already translated from the host's clock)."""
		now = time.monotonic()
		expected = position_ms + ((now - reference_local) * 1000.0 if playing else 0.0)
		expected = max(0.0, expected)

		with self._lock:
			if not self._started:
				self._started = True
				self._set_playhead(expected, playing, now)
				self._seek(expected)
				log.info(f"Timeline: {self.label} joined at {expected / 1000.0:.2f}s "
				      f"({'playing' if playing else 'paused'})")
				self._start_thread()
				return

			local = self._position(now)
			error = expected - local

			if playing != self._playing:
				# Pause or resume. Land exactly on the host's position.
				self._set_playhead(expected, playing, now)
				if abs(error) > self.CORRECTION_THRESHOLD_MS:
					self._seek(expected)
				log.info(f"Timeline: {self.label} {'resumed' if playing else 'paused'} "
				      f"at {expected / 1000.0:.2f}s")
				return

			if abs(error) > self.CORRECTION_THRESHOLD_MS:
				self.corrections += 1
				log.info(f"Timeline: {self.label} correcting {error:+.0f}ms "
				      f"(now {expected / 1000.0:.2f}s, correction #{self.corrections})")
				self._set_playhead(expected, playing, now)
				self._seek(expected)

	def stop(self) -> None:
		"""Stop and release every key this timeline holds on."""
		self._stop_event.set()
		if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
			self._thread.join(timeout=1.0)
		with self._lock:
			for key, value in list(self._state.items()):
				if value:
					self._send(key, 0)
			self._state.clear()

	def position_ms(self) -> float:
		with self._lock:
			return self._position(time.monotonic())

	# -------------------------------------------------------------------------
	# Internal
	# -------------------------------------------------------------------------

	def _start_thread(self) -> None:
		self._thread = threading.Thread(target=self._worker, daemon=True)
		self._thread.start()

	def _position(self, now: float) -> float:
		if self._playing:
			return (now - self._origin) * 1000.0
		return self._frozen_ms

	def _set_playhead(self, position_ms: float, playing: bool, now: float) -> None:
		self._playing = playing
		self._frozen_ms = position_ms
		self._origin = now - position_ms / 1000.0

	def _perform_ms(self, position_ms: float) -> float:
		return position_ms - self.sync_offset_ms

	def _send(self, key, value: int) -> None:
		self._state[key] = value
		try:
			self.dispatch(key, value)
		except Exception as e:
			log.exception(f"Timeline: dispatch failed for {key}={value}: {e}")

	def _seek(self, position_ms: float) -> None:
		"""Jump to position_ms: dispatch whatever differs from what should
		already be held there, and continue from the next event."""
		target_ms = self._perform_ms(position_ms)
		index = bisect.bisect_right(self._times, target_ms)
		target: Dict[object, int] = {}
		for _t, key, value in self.events[:index]:
			target[key] = value
		for key in set(self._state) | set(target):
			value = target.get(key, 0)
			if self._state.get(key, 0) != value:
				self._send(key, value)
		self._index = index

	def _worker(self) -> None:
		while not self._stop_event.is_set():
			with self._lock:
				if self._playing:
					target_ms = self._perform_ms(self._position(time.monotonic()))
					while self._index < len(self.events) and self.events[self._index][0] <= target_ms:
						_t, key, value = self.events[self._index]
						if self._state.get(key, 0) != value:
							self._send(key, value)
						self._index += 1
			time.sleep(self.TICK_S)


# =============================================================================
# Group shows — shows shared between characters
# =============================================================================
#
# Host side: whichever character starts a show hosts it. The host starts its
# song immediately, pushes the full event list to every peer over HTTP
# (gzipped JSON, one thread per peer, never waiting on any of them), then sends
# a heartbeat over UDP every HEARTBEAT_S carrying its playhead position, its
# monotonic clock, and whether it is playing or paused. Stopping sends "end".
#
# Peer side: every character receives every show. One whose MIDI notes /
# ProgramBlue channels don't appear in it ignores it. One whose do joins —
# seeking straight to the host's current position — and follows the
# heartbeats with a ClockedTimeline. If the heartbeats stop for
# HEARTBEAT_TIMEOUT_S, it assumes the host is gone and leaves.
#
# Busy rules: a character that is hosting rejects other hosts' shows. A
# character performing in one host's show rejects every other host's, but
# accepts a replacement show from its own host.


SHOW_TYPE_MIDI = "midi"
SHOW_TYPE_PROGRAM_BLUE = "program_blue"

HEARTBEAT_S = 0.25
HEARTBEAT_TIMEOUT_S = 2.0
# Heartbeats after "end" are gone, so "end" is repeated in case one is lost.
END_REPEATS = 3
PUSH_TIMEOUT_S = 5.0
FETCH_TIMEOUT_S = 5.0

PATH_SHOW = "/peer/show"
PATH_SESSION = "/peer/session"


def encode_payload(payload: dict) -> bytes:
	return gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


class _PeerSession:
	def __init__(self, sid: str, host_serial: str, host_name: str, host_ip: str,
	             show: str, timeline: ClockedTimeline) -> None:
		self.sid = sid
		self.host_serial = host_serial
		self.host_name = host_name
		self.host_ip = host_ip
		self.show = show
		self.timeline = timeline
		self.last_heartbeat = time.monotonic()


class GroupShowManager:
	def __init__(
		self,
		identity,
		peer_network,
		hardware_path: str,
		is_hosting: Callable[[], bool],
		on_join: Callable[[dict], None],
		on_leave: Callable[[str], None],
	) -> None:
		self.identity = identity
		self.peer_network = peer_network
		self.is_hosting = is_hosting
		self.on_join = on_join
		self.on_leave = on_leave

		self.owned_notes: set = set()
		self.owned_channels: set = set()
		self.sync_offset_ms: float = 0.0
		self._load_hardware(hardware_path)

		self._lock = threading.RLock()

		# Host side.
		self._host: Optional[dict] = None
		self._host_state_fn: Optional[Callable[[], Tuple[float, bool]]] = None
		self._heartbeat_thread: Optional[threading.Thread] = None

		# Peer side.
		self._peer: Optional[_PeerSession] = None
		self._ignored_sids: collections.deque = collections.deque(maxlen=64)
		self._fetching: set = set()
		# Sessions between accept and timeline start, so a heartbeat arriving
		# in that window doesn't trigger a second join.
		self._joining: set = set()

		threading.Thread(target=self._watchdog, daemon=True).start()

	# -------------------------------------------------------------------------
	# Config
	# -------------------------------------------------------------------------

	def _load_hardware(self, hardware_path: str) -> None:
		try:
			with open(hardware_path, "r") as f:
				config = json.load(f)
		except (OSError, ValueError) as e:
			log.exception(f"GroupShow: could not read character config '{hardware_path}': {e}")
			return
		for m in config.get("movements", []):
			note = m.get("midi_note")
			if isinstance(note, int) and note > 0:
				self.owned_notes.add(note)
			channel = m.get("program_blue_channel")
			if isinstance(channel, int) and channel >= 0:
				self.owned_channels.add(channel)
		try:
			self.sync_offset_ms = float(config.get("show_sync_offset_ms", 0) or 0)
		except (TypeError, ValueError):
			log.warning("GroupShow: show_sync_offset_ms is not a number, using 0.")
		log.info(f"GroupShow: owns MIDI notes {sorted(self.owned_notes)}, "
		      f"ProgramBlue channels {sorted(self.owned_channels)}, "
		      f"sync offset {self.sync_offset_ms:+.0f}ms")

	def info(self) -> dict:
		"""The /peer/info response."""
		return {
			"name": self.identity.name,
			"description": self.identity.description,
			"serial": self.identity.serial,
			"ip": self.peer_network.ip,
			"midi_notes": sorted(self.owned_notes),
			"program_blue_channels": sorted(self.owned_channels),
		}

	# -------------------------------------------------------------------------
	# State queries
	# -------------------------------------------------------------------------

	def is_performing(self) -> bool:
		"""True while performing in another character's show."""
		with self._lock:
			return self._peer is not None

	def performing_for(self) -> Optional[str]:
		with self._lock:
			return self._peer.host_name if self._peer else None

	# -------------------------------------------------------------------------
	# Host side
	# -------------------------------------------------------------------------

	def start_hosting(self, show_name: str, show_type: str, events: List[List],
	                  state_fn: Callable[[], Tuple[float, bool]]) -> None:
		"""Begin (or replace) this character's hosted show and push it to
		every peer. state_fn returns (position_ms, playing)."""
		sid = uuid.uuid4().hex[:12]
		with self._lock:
			self._host = {
				"sid": sid,
				"show": show_name,
				"type": show_type,
				"events": events,
			}
			self._host_state_fn = state_fn
			if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
				self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
				self._heartbeat_thread.start()

		peers = self.peer_network.peers()
		log.info(f"GroupShow: hosting '{show_name}' (session {sid}, {len(events)} events) "
		      f"— pushing to {len(peers)} peer(s).")
		if not peers:
			return
		body = encode_payload(self.session_payload())
		for peer in peers:
			threading.Thread(target=self._push, args=(peer, body), daemon=True).start()

	def stop_hosting(self) -> None:
		with self._lock:
			host = self._host
			self._host = None
			self._host_state_fn = None
		if host is None:
			return
		log.info(f"GroupShow: hosted show '{host['show']}' ended (session {host['sid']}).")
		message = {"t": "end", "sid": host["sid"], "host": self.identity.serial}
		threading.Thread(target=self._send_end, args=(message,), daemon=True).start()

	def session_payload(self) -> Optional[dict]:
		"""The current hosted session, with the playhead as of now."""
		with self._lock:
			host = self._host
			state_fn = self._host_state_fn
		if host is None or state_fn is None:
			return None
		position, playing = state_fn()
		return {
			"sid": host["sid"],
			"host": {"serial": self.identity.serial, "name": self.identity.name},
			"show": host["show"],
			"type": host["type"],
			"events": host["events"],
			"pos": position,
			"hm": time.monotonic(),
			"st": "play" if playing else "pause",
		}

	def on_peer_added(self, peer) -> None:
		"""A character appeared mid-show (booted late, reconnected): send it
		the current show so it can seek in."""
		payload = self.session_payload()
		if payload is None:
			return
		log.info(f"GroupShow: '{peer.name}' appeared mid-show, sending it the session.")
		threading.Thread(target=self._push, args=(peer, encode_payload(payload)), daemon=True).start()

	def _push(self, peer, body: bytes) -> None:
		url = f"http://{peer.ip}:{peer.web_port}{PATH_SHOW}"
		try:
			response = requests.post(url, data=body, timeout=PUSH_TIMEOUT_S, headers={
				"Content-Type": "application/json",
				"Content-Encoding": "gzip",
			})
			result = response.json() if response.content else {}
			if result.get("accepted"):
				log.info(f"GroupShow: '{peer.name}' joined.")
			else:
				log.info(f"GroupShow: '{peer.name}' not joining ({result.get('reason', 'no reason given')}).")
		except Exception as e:
			log.warning(f"GroupShow: push to '{peer.name}' at {peer.ip} failed: {e}")

	def _heartbeat_loop(self) -> None:
		while True:
			with self._lock:
				host = self._host
				state_fn = self._host_state_fn
			if host is None or state_fn is None:
				return
			try:
				position, playing = state_fn()
			except Exception as e:
				log.exception(f"GroupShow: could not read host position: {e}")
				position, playing = 0.0, False
			message = {
				"t": "hb",
				"sid": host["sid"],
				"host": self.identity.serial,
				"pos": position,
				"hm": time.monotonic(),
				"st": "play" if playing else "pause",
			}
			for peer in self.peer_network.peers():
				self.peer_network.send_to_peer(peer, message)
			time.sleep(HEARTBEAT_S)

	def _send_end(self, message: dict) -> None:
		for _ in range(END_REPEATS):
			for peer in self.peer_network.peers():
				self.peer_network.send_to_peer(peer, message)
			time.sleep(0.1)

	# -------------------------------------------------------------------------
	# Peer side
	# -------------------------------------------------------------------------

	def receive_push(self, payload: dict, source_ip: str) -> dict:
		"""A host sent a show. Runs on the HTTP thread; the returned dict goes
		back to the host."""
		try:
			sid = str(payload["sid"])
			host_serial = str(payload["host"]["serial"])
			host_name = str(payload["host"].get("name", host_serial))
			show_type = str(payload["type"])
			events = payload["events"]
			position = float(payload.get("pos", 0.0))
			host_mono = float(payload["hm"])
			playing = payload.get("st", "play") == "play"
		except (KeyError, TypeError, ValueError) as e:
			log.warning(f"GroupShow: malformed show from {source_ip}: {e}")
			return {"accepted": False, "reason": "malformed"}
		received = time.monotonic()

		if host_serial == self.identity.serial:
			return {"accepted": False, "reason": "own show"}
		if self.is_hosting():
			log.info(f"GroupShow: ignoring show from '{host_name}' — hosting my own.")
			return {"accepted": False, "reason": "hosting"}

		leaving = None
		with self._lock:
			current = self._peer
			if current is not None and current.host_serial != host_serial:
				log.info(f"GroupShow: ignoring show from '{host_name}' — "
				      f"performing in '{current.host_name}'s show.")
				return {"accepted": False, "reason": "busy"}
			if (current is not None and current.sid == sid) or sid in self._joining:
				return {"accepted": True, "reason": "already joined"}

			mine = self._filter_events(show_type, events)
			if not mine:
				self._ignored_sids.append(sid)
				if current is not None:
					# My host moved on to a show I'm not in.
					leaving = self._detach_locked()
			else:
				self._joining.add(sid)
		if not mine:
			log.info(f"GroupShow: '{payload.get('show')}' from '{host_name}' has nothing for me.")
			if leaving is not None:
				self._finish_leave(leaving, "host switched to a show without me")
			return {"accepted": False, "reason": "no events"}

		timeline = ClockedTimeline(mine, self._make_dispatch(show_type), self.sync_offset_ms,
			label=f"'{payload.get('show')}' from '{host_name}'")

		if current is not None:
			# Replacement from the same host: stay in show mode and swap the
			# timeline. The old one releases what it held first.
			current.timeline.stop()
			log.info(f"GroupShow: '{host_name}' replaced its show with '{payload.get('show')}'.")
		else:
			# Outside the lock: entering show mode stops speech, which waits on
			# threads that may themselves ask this manager for its state.
			try:
				self.on_join({"host_name": host_name, "show": payload.get("show")})
			except Exception as e:
				log.exception(f"GroupShow: on_join failed: {e}")

		with self._lock:
			self._joining.discard(sid)
			self._peer = _PeerSession(sid, host_serial, host_name, source_ip,
				str(payload.get("show", "")), timeline)
			offset = self._host_offset(host_serial, host_mono, received)
			timeline.update_reference(position, host_mono - offset, playing)
		log.info(f"GroupShow: performing {len(mine)} of {len(events)} events in "
		      f"'{payload.get('show')}' from '{host_name}' "
		      f"(clock offset {offset * 1000.0:+.1f}ms"
		      f"{'' if self.peer_network.offset(host_serial) is not None else ', estimated'}).")
		return {"accepted": True}

	def on_message(self, message: dict, address: Tuple[str, int]) -> None:
		"""UDP messages that aren't clock sync: heartbeats and ends."""
		kind = message.get("t")
		sid = str(message.get("sid", ""))
		host_serial = str(message.get("host", ""))
		if not sid or host_serial == self.identity.serial:
			return

		if kind == "end":
			leaving = None
			with self._lock:
				if self._peer is not None and self._peer.sid == sid:
					leaving = self._detach_locked()
			if leaving is not None:
				self._finish_leave(leaving, "host ended the show")
			return

		if kind != "hb":
			return

		try:
			position = float(message["pos"])
			host_mono = float(message["hm"])
			received = float(message.get("_rx", time.monotonic()))
		except (KeyError, TypeError, ValueError):
			return
		playing = message.get("st") == "play"

		with self._lock:
			session = self._peer
			if session is not None and session.sid == sid:
				session.last_heartbeat = time.monotonic()
				offset = self._host_offset(host_serial, host_mono, received)
				session.timeline.update_reference(position, host_mono - offset, playing)
				return
			if sid in self._ignored_sids or sid in self._fetching or sid in self._joining:
				return
			if session is not None and session.host_serial != host_serial:
				return
			self._fetching.add(sid)

		# A heartbeat for a show we never received — the push was lost, or
		# we came up after it went out. Ask the host for it.
		threading.Thread(target=self._fetch_session, args=(sid, address[0]), daemon=True).start()

	def _fetch_session(self, sid: str, host_ip: str) -> None:
		try:
			if self.is_hosting():
				return
			response = requests.get(f"http://{host_ip}{PATH_SESSION}", timeout=FETCH_TIMEOUT_S)
			if response.status_code != 200:
				return
			payload = response.json()
			self.receive_push(payload, host_ip)
		except Exception as e:
			log.warning(f"GroupShow: could not fetch session {sid} from {host_ip}: {e}")
		finally:
			with self._lock:
				self._fetching.discard(sid)
				# Whatever happened, don't ask about this session again.
				if self._peer is None or self._peer.sid != sid:
					if sid not in self._ignored_sids:
						self._ignored_sids.append(sid)

	def leave(self, reason: str) -> None:
		with self._lock:
			session = self._detach_locked()
		if session is not None:
			self._finish_leave(session, reason)

	def _detach_locked(self) -> Optional[_PeerSession]:
		session = self._peer
		self._peer = None
		return session

	def _finish_leave(self, session: _PeerSession, reason: str) -> None:
		"""Stop the timeline and tell start.py. Always called outside the lock."""
		session.timeline.stop()
		log.info(f"GroupShow: left '{session.show}' from '{session.host_name}' — {reason} "
		      f"({session.timeline.corrections} correction(s)).")
		try:
			self.on_leave(reason)
		except Exception as e:
			log.exception(f"GroupShow: on_leave failed: {e}")

	def _watchdog(self) -> None:
		while True:
			time.sleep(0.25)
			leaving = None
			with self._lock:
				session = self._peer
				if session is not None and time.monotonic() - session.last_heartbeat > HEARTBEAT_TIMEOUT_S:
					leaving = self._detach_locked()
			if leaving is not None:
				self._finish_leave(leaving, "lost the host's heartbeat")

	# -------------------------------------------------------------------------
	# Helpers
	# -------------------------------------------------------------------------

	def _host_offset(self, host_serial: str, host_mono: float, received: float) -> float:
		"""Host's monotonic clock minus ours. Measured by clock sync when
		available; otherwise estimated from this message, ignoring its
		(small) transit time."""
		offset = self.peer_network.offset(host_serial)
		if offset is not None:
			return offset
		return host_mono - received

	def _filter_events(self, show_type: str, events: list) -> List[List]:
		if show_type == SHOW_TYPE_MIDI:
			owned = self.owned_notes
		elif show_type == SHOW_TYPE_PROGRAM_BLUE:
			owned = self.owned_channels
		else:
			log.warning(f"GroupShow: unknown show type '{show_type}'.")
			return []
		mine = []
		for event in events:
			try:
				t, key, value = event
			except (TypeError, ValueError):
				continue
			if key in owned:
				mine.append([t, key, value])
		return mine

	@staticmethod
	def _make_dispatch(show_type: str):
		# The same signals ShowPlayer uses for a local show, so Movement
		# handles them identically (output muted, no MIDI/ProgramBlue echo).
		if show_type == SHOW_TYPE_MIDI:
			def dispatch(key, value):
				dispatcher.send(signal="onMidiEvent", midi_note=key, val=value)
		else:
			def dispatch(key, value):
				dispatcher.send(signal="onProgramBlueEvent", channel=key, val=value)
		return dispatch
