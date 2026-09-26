#!/usr/bin/env python3
import os
import re
import sys
import time
import warnings
import subprocess

# Suppress noise — must be set before any import that touches audio, pygame
# included: it reads PYGAME_HIDE_SUPPORT_PROMPT at import time, so setting it
# after the import had no effect.
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "1"
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['SDL_AUDIODRIVER'] = 'alsa'
os.environ["PYTHONWARNINGS"] = "ignore"
os.environ["ORT_LOGGING_LEVEL"] = "3"
if 'XDG_RUNTIME_DIR' not in os.environ:
	os.environ['XDG_RUNTIME_DIR'] = "/tmp"
warnings.filterwarnings("ignore")

# Logging comes up before any other project module: several of them log
# while they import, and those lines matter when diagnosing a boot.
import logger

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = logger.setup_logging(_BASE_DIR)
log = logger.get_logger("start")

import usb_monitor
import audio_setup
import mic_stream
import respeaker
import pygame

# Suppress all stderr noise (ALSA, onnxruntime, pyaudio) during startup
_devnull = open(os.devnull, 'w')
_old_stderr = os.dup(2)
os.dup2(_devnull.fileno(), 2)

# Init pygame mixer — playback runs through the on-board APE card driving the
# PCM5102 I2S DAC. Route the AHUB crossbar (does not survive reboot) and locate
# the APE device, retrying until it's ready. The ReSpeaker USB mic is handled
# separately (respeaker / mic_stream) and is input-only.
pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=4096)

for attempt in range(30):
	audio_card = audio_setup.init_audio()
	if audio_card:
		os.environ['AUDIODEV'] = audio_card
		try:
			pygame.mixer.init()
			break
		except Exception as e:
			log.exception(f"Audio: mixer init failed (attempt {attempt + 1}/30): {e}, retrying...")
	else:
		log.warning(f"Audio: APE device not found (attempt {attempt + 1}/30), retrying...")
	time.sleep(2)
else:
	log.warning("Audio: APE audio device not found after 30 attempts. Exiting.")
	sys.exit(1)

respeaker.initialize()

# Now safe to import everything else
import signal
import threading
import ctypes
import configparser
import json
import utils
from pydispatch import dispatcher
from web_io import WebServer
from wakeword_detection import WakeWord
from speech_to_text import SpeechToText
from text_to_speech import TextToSpeech
from voice_commands import VoiceCommandHandler
from llm_service import LLM
from voice_player import VoicePlayer
from animation_controller import AnimationController
from led_controller import LEDController
from animatronic_movements import Movement
from show_player import GroupShowManager, ShowPlayer
from show_upload import ShowUploader
from wifi_management import WifiManagement
from utils import Identity, cancel_conversation, get_local_ip
from peer_network import PeerConversation, PeerNetwork

# Restore stderr now that all noisy imports are done
os.dup2(_old_stderr, 2)
os.close(_old_stderr)
_devnull.close()

log.info("Imports complete.")


def _load_hardware_config(config_path: str) -> dict:
	"""Read the hardware JSON path from config.cfg, load and return its contents. Exits if missing or not found."""
	cfg = configparser.ConfigParser()
	cfg.read(config_path)
	json_path = cfg.get("Hardware", "config", fallback="").strip()
	if not json_path:
		log.critical("Error: 'config' not specified under [Hardware] in config.cfg. Cannot proceed.")
		logger.flush()
		sys.exit(1)
	abs_path = os.path.join(_BASE_DIR, json_path)
	if not os.path.exists(abs_path):
		log.critical(f"Error: Hardware config not found at '{abs_path}'. Cannot proceed.")
		logger.flush()
		sys.exit(1)
	with open(abs_path, 'r') as f:
		hardware = json.load(f)
	hardware['_path'] = abs_path
	return hardware


class Kermit:
	def __init__(self) -> None:
		self.is_running: bool = True
		self.wifi_access_points = None
		self._awaiting_followup: bool = False
		self._prev_show_status: str = "stopped"
		self._prev_status_id: str = ""
		self._prev_status_value: any = None
		self._config_data: dict = {}
		self._key_map: list = []
		self.shows_dir: str = os.path.join(_BASE_DIR, utils.SHOWS_DIRNAME)
		# None, "host" (playing a show of our own, shared with peers) or
		# "peer" (performing in another character's show).
		self._show_mode = None
		self._peer_list: list = []

		# Resolve where the config and shows live (USB-first, bootstrapping
		# from the template if needed) before anything reads them. The
		# hardware JSON path comes from the config, so startup cannot
		# proceed without one.
		self.config_path, self.shows_dir, using_usb = utils.resolve_storage(
			_BASE_DIR, usb_monitor.USB_MOUNT_POINT, usb_monitor.is_mounted()
		)
		if not self.config_path:
			log.critical("Error: No usable config.cfg found and none could be created from config_template.cfg. Cannot proceed.")
			logger.flush()
			sys.exit(1)
		hardware = _load_hardware_config(self.config_path)
		self.identity = Identity(hardware['_path'])

		logger.log_boot_banner(
			_BASE_DIR,
			identity=f"{self.identity.name} ({self.identity.serial})",
			character=hardware.get('_path', 'unknown'),
			config=f"{self.config_path} ({'USB' if using_usb else 'local backup'})",
			shows=self.shows_dir,
			log_file=_LOG_PATH,
		)
		self._key_map = self._build_key_map(hardware)

		wakeword_model  = os.path.join(_BASE_DIR, hardware['wakeword']['model'])
		wakeword_desc   = hardware['wakeword']['description']
		voices_dir      = os.path.join(_BASE_DIR, hardware['voice_directory'])
		animation_dir   = os.path.join(_BASE_DIR, hardware.get('animation_directory', ''))
		hardware_path   = hardware['_path']
		html_config     = hardware.get('html', {})
		busy_sound      = str(hardware.get('busy_sound', '') or '').strip()

		# Initialize components
		self.wakeword = WakeWord(model_path=wakeword_model, description=wakeword_desc)
		self.stt = SpeechToText()
		self.tts = TextToSpeech(hardware_path)
		self.llm = LLM()
		self.voice_player = VoicePlayer(pygame, voices_dir=voices_dir, hardware_path=hardware_path)
		self.animation_controller = AnimationController(animation_dir=animation_dir)
		self.led_controller = LEDController(hardware_path)
		self.movements = Movement(hardware_path)
		self.web_server = WebServer(html_config)
		self.wifi_management = WifiManagement()
		self.show_player = ShowPlayer(pygame)
		self.show_player.set_busy_sound(os.path.join(_BASE_DIR, busy_sound) if busy_sound else "")
		self.show_uploader = ShowUploader(_BASE_DIR, hardware_path)
		self.voiceCommandHandler = VoiceCommandHandler(self.wifi_management, self.show_player)

		# Uploads come in over HTTP rather than the socket, so the web server
		# needs the handler directly instead of a dispatcher signal.
		self.web_server.set_upload_handler(self.on_show_upload)

		# Group shows: discovery + clock sync, and the session manager that
		# hosts our shows and performs in other characters'.
		self.peer_network = PeerNetwork(
			self.identity,
			on_peers_changed=self.on_peers_changed,
			on_peer_added=lambda peer: self.group_show.on_peer_added(peer),
			on_message=lambda message, address: self.group_show.on_message(message, address),
		)
		self.group_show = GroupShowManager(
			self.identity,
			self.peer_network,
			hardware_path,
			is_hosting=lambda: self._show_mode == "host",
			on_join=self.on_group_show_join,
			on_leave=self.on_group_show_leave,
		)
		# Shares LLM responses with the other characters and coordinates who
		# speaks which line. Ignores responses while a show is running.
		self.peer_conversation = PeerConversation(
			self.identity,
			self.peer_network,
			can_speak=lambda: self._show_mode is None,
		)
		self.web_server.set_peer_handlers(
			self.group_show.info,
			self.group_show.receive_push,
			self.group_show.session_payload,
			self.peer_conversation.receive,
		)
		ip, interface = get_local_ip()
		self.peer_network.start(ip, interface)

		self.set_dispatch_events()
		self.wakeword.set_enabled(True)
		self.wifi_management.scan()
		self.load_config()

		# Handle SIGINT and SIGTERM for graceful shutdown
		signal.signal(signal.SIGINT, self.shutdown)
		signal.signal(signal.SIGTERM, self.shutdown)

		self.led_controller.set_state(LEDController.STATE_OFF)

	def set_dispatch_events(self) -> None:
		dispatcher.connect(self.on_update_status, signal='updateStatus', sender=dispatcher.Any)
		dispatcher.connect(self.on_connect_event, signal='connectEvent', sender=dispatcher.Any)
		dispatcher.connect(self.on_movement_key_activated, signal='onMovementKeyActivated', sender=dispatcher.Any)
		dispatcher.connect(self.load_config, signal='usbAttached', sender=dispatcher.Any)
		dispatcher.connect(self.on_usb_detached, signal='usbDetached', sender=dispatcher.Any)
		dispatcher.connect(self.on_restore_backup, signal='restoreBackup', sender=dispatcher.Any)
		dispatcher.connect(self.on_wakeword_event, signal='wakewordEvent', sender=dispatcher.Any)
		dispatcher.connect(self.on_transcription_result, signal='transcriptionResult', sender=dispatcher.Any)
		dispatcher.connect(self.on_execute_text_to_speech, signal='executeTTS', sender=dispatcher.Any)
		dispatcher.connect(self.on_voice_play, signal='playVoiceFile', sender=dispatcher.Any)
		dispatcher.connect(self.on_voice_play_sequence, signal='playVoiceSequence', sender=dispatcher.Any)
		dispatcher.connect(self.on_voice_playback_event, signal='voicePlaybackEvent', sender=dispatcher.Any)
		dispatcher.connect(self.on_show_list_load, signal='showListLoad', sender=dispatcher.Any)
		dispatcher.connect(self.on_show_status, signal='showStatus', sender=dispatcher.Any)
		dispatcher.connect(self.on_connect_to_wifi_network, signal='connectToWifi', sender=dispatcher.Any)
		dispatcher.connect(self.on_wifi_scan_complete, signal='wifiScanComplete', sender=dispatcher.Any)
		dispatcher.connect(self.on_wifi_connected, signal='wifiConnected', sender=dispatcher.Any)
		dispatcher.connect(self.on_config_save, signal='configSave', sender=dispatcher.Any)
		dispatcher.connect(self.on_convert_show, signal='convertShow', sender=dispatcher.Any)

	def load_config(self, path: str = "", apply_wifi: bool = True) -> None:
		"""Resolve where the config and shows live (USB first, then the local
		backup — see utils.resolve_storage) and apply the result. `path` is an
		explicit USB config path from the usbAttached event. WiFi apply can be
		skipped, since it initiates a connection attempt."""
		resolved, shows_dir, using_usb = utils.resolve_storage(
			_BASE_DIR, usb_monitor.USB_MOUNT_POINT, usb_monitor.is_mounted(),
			usb_config_path=path,
		)
		self.shows_dir = shows_dir
		self.show_player.set_show_directory(shows_dir)

		# Any drive that is present leaves with the current documentation.
		docs = utils.copy_docs_to_usb(
			_BASE_DIR, usb_monitor.USB_MOUNT_POINT, usb_monitor.is_mounted()
		)
		if docs:
			log.info(f"Docs: {docs} document(s) on the USB drive are up to date.")

		if not resolved:
			self.config_path = None
			log.warning("Warning: No usable config found and none could be created. Continuing with no config.")
			return

		self.config_path = resolved
		log.info(f"Config loaded from {resolved} ({'USB' if using_usb else 'local backup'})")
		if apply_wifi:
			self.wifi_management.apply_config(resolved)
		self.llm.apply_config(resolved)
		self.tts.apply_config(resolved)
		self.wakeword.apply_config(resolved)
		self.led_controller.apply_config(resolved)
		self._config_data = self._build_config_data(resolved)
		self.web_server.broadcast('configLoaded', self._config_data)

	# Sections excluded from the web broadcast — WiFi already has its own
	# dedicated UI (scan list + connect popup), and Hardware (the character
	# JSON path) isn't something to edit from the web page.
	BROADCAST_EXCLUDED_SECTIONS = ("wifi", "hardware")

	def _build_config_data(self, path: str) -> dict:
		"""Parse the config file into {section: {key: value}} for the web UI.
		The template fills in keys added after a config was written, so an
		older config still offers newer settings."""
		return utils.build_config_data(
			path, self.BROADCAST_EXCLUDED_SECTIONS,
			template_path=os.path.join(_BASE_DIR, utils.TEMPLATE_FILENAME),
		)

	def _build_key_map(self, hardware: dict) -> list:
		"""Build the movement key list for the web keypad grid from the
		character config's movements. Each entry carries the keyboard key, the
		movement's gamepad buttons (kept for later use in the UI), and the
		human-readable description. Movements with no keyboard key are skipped."""
		key_map = []
		for m in hardware.get('movements', []):
			key = str(m.get('key', '')).strip()
			if not key:
				continue
			key_map.append({
				'key': key,
				'gamepad_buttons': m.get('gamepad_buttons', []),
				'description': m.get('description', ''),
			})
		return key_map

	def on_config_save(self, updates: dict) -> None:
		"""Handle config edits from the web UI: write to disk, re-apply,
		and rebroadcast the config so all connected clients stay in sync."""
		if not isinstance(updates, dict) or not updates:
			self.web_server.broadcast('configSaveResult', {'success': False, 'error': 'No updates provided.'})
			return
		if not self.config_path:
			self.web_server.broadcast('configSaveResult', {'success': False, 'error': 'No config file loaded.'})
			return

		try:
			utils.write_config_values(self.config_path, updates)
		except Exception as e:
			# Most likely a read-only mount (USB stick) or permissions.
			log.exception(f"Config: save failed: {e}")
			self.web_server.broadcast('configSaveResult', {'success': False, 'error': str(e)})
			return

		log.info(f"Config: saved {sum(len(v) for v in updates.values() if isinstance(v, dict))} value(s) to {self.config_path}")

		# Mirror the saved config to the other location(s): local dir + USB.
		sync_errors = utils.sync_config_copies(
			self.config_path, _BASE_DIR, usb_monitor.USB_MOUNT_POINT, usb_monitor.is_mounted()
		)
		for err in sync_errors:
			log.info(f"Config: {err}")

		# Reload: re-applies components and rebroadcasts configLoaded to all
		# clients. WiFi apply is skipped unless its section actually changed,
		# since it initiates a connection attempt (the web editor doesn't
		# include WiFi, so normally it never does).
		wifi_changed = any(section.strip().lower() == 'wifi' for section in updates)
		self.load_config(apply_wifi=wifi_changed)

		result = {'success': True}
		if sync_errors:
			result['warning'] = "Saved, but couldn't sync all copies: " + "; ".join(sync_errors)
		self.web_server.broadcast('configSaveResult', result)

	def on_usb_detached(self) -> None:
		"""The drive is gone — fall back to the local backup for everything."""
		log.warning("Config: USB drive removed, falling back to the local backup.")
		self.load_config(apply_wifi=False)

	def on_restore_backup(self) -> None:
		"""Write the local backup onto an attached USB drive, then reload so
		the drive becomes the source of truth. Runs off the socket thread —
		copying shows can take minutes."""
		def restore():
			success, message = utils.restore_backup_to_usb(
				_BASE_DIR, usb_monitor.USB_MOUNT_POINT, usb_monitor.is_mounted()
			)
			if success:
				self.load_config(apply_wifi=False)
			else:
				log.info(f"Restore: {message}")
			self.web_server.broadcast('restoreBackupResult',
				{'success': success, 'message': message})
		threading.Thread(target=restore, daemon=True).start()

	def run(self) -> None:
		try:
			while self.is_running:
				time.sleep(0.005)
		except Exception as e:
			log.exception(f"Error in main loop: {e}")
		finally:
			log.info("Main loop exiting, calling shutdown...")
			self.shutdown()

	def shutdown(self, *args) -> None:
		try:
			self.is_running = False

			if self.web_server:
				self.web_server.shutdown()

			if self.show_player:
				self.show_player.stop_show()

			# Tell peers the show is over and send the mDNS goodbye, so other
			# characters drop us straight away rather than on a timeout.
			group_show = getattr(self, "group_show", None)
			if group_show:
				group_show.stop_hosting()
				group_show.leave("shutting down")
			peer_network = getattr(self, "peer_network", None)
			if peer_network:
				peer_network.stop()

			if self.stt:
				self.stt.shutdown()

			# Close the shared capture stream so arecord doesn't outlive us.
			mic_stream.stop()

			for thread in threading.enumerate():
				if thread is not threading.main_thread():
					if thread.is_alive():
						try:
							ctypes.pythonapi.PyThreadState_SetAsyncExc(
								ctypes.c_long(thread.ident), ctypes.py_object(SystemExit)
							)
						except Exception as e:
							log.exception(f"Error stopping thread {thread.name}: {e}")

			pygame.mixer.quit()
			pygame.display.quit()
			pygame.quit()

			log.info("Shutdown complete. Exiting.")
			sys.exit(0)

		except Exception as e:
			log.exception(f"Error during shutdown: {e}")
			sys.exit(1)

	def on_show_upload(self, files: list) -> dict:
		"""Write an uploaded show set to storage and refresh the show list.
		Runs on the HTTP thread, so the result is returned to the caller
		rather than broadcast."""
		result = self.show_uploader.upload(files)
		if result.get('success'):
			self.show_player.get_show_list()
		return result

	def on_convert_show(self, show_name: str) -> None:
		"""Convert an uploaded MIDI show to ProgramBlue. Runs off the socket
		thread — the converter transcodes the whole audio track."""
		def convert():
			result = self.show_uploader.convert_to_program_blue(show_name)
			if result.get('success'):
				self.show_player.get_show_list()
			self.web_server.broadcast('showConvertResult', result)
		threading.Thread(target=convert, daemon=True).start()

	def on_show_list_load(self, show_list: any) -> None:
		self.web_server.broadcast('showListLoaded', show_list)

	def on_show_status(self, status: str, show_name: str = "") -> None:
		if self._show_mode == "peer":
			# Performing in another character's show: that host owns the
			# transport. A request to play something else gets the busy
			# sound; pause and stop are simply ignored.
			if status == "play":
				log.info(f"Show: busy performing in {self.group_show.performing_for()}'s show, "
				      f"ignoring request to play '{show_name}'.")
				self.show_player.play_busy_sound()
			self.web_server.broadcast('showStatusUpdated', self._prev_show_status)
			return

		self._prev_show_status = status
		self.web_server.broadcast('showStatusUpdated', status)
		if status == "play":
			resuming = (self.show_player.paused and self.show_player.active_show_name is not None
			            and show_name == self.show_player.active_show_name)
			if resuming:
				# load_show unpauses a paused show of the same name. The
				# heartbeat carries the resume to peers.
				self.show_player.load_show(show_name)
				self.wakeword.set_enabled(False)
				return

			self.enter_show_mode("host", f"playing '{show_name or 'a random show'}'")
			self.show_player.load_show(show_name)
			# Also covers replacing a paused show, when the wakeword was on.
			self.wakeword.set_enabled(False)
			if not self.show_player.is_active():
				log.warning(f"Show: '{show_name}' did not start.")
				self.group_show.stop_hosting()
				self.exit_show_mode()
				self._prev_show_status = "stop"
				self.web_server.broadcast('showStatusUpdated', "stop")
				return
			# Starting over a show we're already hosting replaces it; peers
			# switch to the new session.
			self.group_show.start_hosting(
				self.show_player.active_show_name,
				self.show_player.show_type_name(),
				self.show_player.show_events,
				self.show_player.get_state,
			)
		elif status == "pause":
			if self._show_mode != "host":
				return
			self.show_player.toggle_pause()
			# pause is a toggle: listen while paused, not after resuming.
			self.wakeword.set_enabled(self.show_player.paused)
		elif status == "stop":
			self.show_player.stop_show()
			self.group_show.stop_hosting()
			self.movements.reset_all()
			self.exit_show_mode()
		elif status == "end":
			self.group_show.stop_hosting()
			self.movements.reset_all()
			self.exit_show_mode()

	# -------------------------------------------------------------------------
	# Show mode
	# -------------------------------------------------------------------------

	def enter_show_mode(self, mode: str, reason: str) -> None:
		"""A show always takes over from a conversation. Cancel everything the
		conversation has in flight, clear the LLM history, and get the
		movements, LEDs and mic into a clean state before the show starts."""
		previous = self._show_mode
		self._show_mode = mode
		if previous is not None:
			# Already in a show (a host replacing its own): nothing to cancel.
			return

		log.info(f"Show mode: entering as {mode} — {reason}.")
		cancel_conversation(reason)
		self._awaiting_followup = False
		self.llm.clear_history()
		self.voice_player.stop()
		dispatcher.send(signal="animationStop")
		self.movements.reset_all(b_notify=True)
		self.wakeword.set_enabled(False)
		self.led_controller.set_state(LEDController.STATE_OFF)
		mic_stream.set_muted(False)

	def exit_show_mode(self, enable_wakeword: bool = True) -> None:
		if self._show_mode is None:
			return
		log.info(f"Show mode: leaving ({self._show_mode}).")
		self._show_mode = None
		self.movements.reset_all(b_notify=True)
		if enable_wakeword:
			self.wakeword.set_enabled(True)

	def on_group_show_join(self, info: dict) -> None:
		"""Another character's show has notes for us. Runs on the HTTP thread
		the host's push arrived on."""
		host = info.get("host_name", "another character")
		show = info.get("show", "")
		self.enter_show_mode("peer", f"performing '{show}' with {host}")
		self.on_update_status("Group Show", f"Performing '{show}' with {host}")

	def on_group_show_leave(self, reason: str) -> None:
		if self._show_mode != "peer":
			return
		self.exit_show_mode()

	def on_peers_changed(self, peers: list) -> None:
		self._peer_list = peers
		self.web_server.broadcast('peersUpdated', peers)

	def on_connect_event(self, client_ip: str) -> None:
		log.info(f"Web client connected from IP: {client_ip}")
		self.web_server.broadcast('voiceCommandUpdate', {"id": "idle", "value": ""})
		self.show_player.get_show_list()
		self.web_server.broadcast('wifiScan', self.wifi_access_points)
		self.web_server.broadcast('showStatusUpdated', self._prev_show_status)
		self.web_server.broadcast('configLoaded', self._config_data)
		self.web_server.broadcast('keyMapLoaded', self._key_map)
		self.web_server.broadcast('peersUpdated', self._peer_list)
		self.on_update_status(self._prev_status_id, self._prev_status_value)
		current_ssid = self.wifi_management.get_current_ssid()
		if current_ssid:
			match = next((n for n in (self.wifi_access_points or []) if n['ssid'] == current_ssid), None)
			self.web_server.broadcast('wifiConnected', {'ssid': current_ssid, 'signal': match['signal_strength'] if match else 0})
		self.wifi_management.scan()

	def on_movement_key_activated(self, key: str, on: bool) -> None:
		"""A movement was activated or released from any source (keyboard,
		gamepad, MIDI, or show playback)."""
		self.web_server.broadcast('movementKeyActivated', {"key": str(key).lower(), "on": bool(on)})

	def on_wakeword_event(self) -> None:
		if self._show_mode == "peer":
			return
		self.led_controller.set_state(LEDController.STATE_LISTENING)
		if self._show_mode == "host":
			# Only reachable while paused — the wakeword is off during
			# playback. Talking ends the show for everyone.
			self.show_player.stop_show()
			self.group_show.stop_hosting()
			self.exit_show_mode(enable_wakeword=False)
			self._prev_show_status = "stop"
			self.web_server.broadcast('showStatusUpdated', "stop")
		def handle():
			self.show_player.stop_show()
			audio_setup.wake_dac_if_needed(pygame)
			dispatcher.send(signal="animationStart", name="wakeword")
			if not self.wakeword.wait_until_stopped(timeout=4.0):
				log.warning("WakeWord: timed out waiting for listen loop to exit, proceeding anyway.")
			self.stt.listen_once()
		threading.Thread(target=handle, daemon=True).start()

	def on_transcription_result(self, text: str) -> None:
		if self._show_mode is not None:
			# Typed from the web UI during a show (the wakeword is off, so
			# nothing is spoken). Commands still work — "stop", or a play
			# request that gets the busy sound — but nothing goes to the LLM.
			if text and text != "[SILENCE]" and not self.voiceCommandHandler.parse(text):
				log.info(f"Heard '{text}' during a show — not a command, ignoring.")
			return
		if not text or text == "[SILENCE]":
			if self._awaiting_followup:
				self._awaiting_followup = False
			self.wakeword.set_enabled(True)
			dispatcher.send(signal="animationStop")
			self.led_controller.set_state(LEDController.STATE_OFF)
			self.movements.reset_all()
			return
		log.info(f"Heard: {text}")
		if not self.voiceCommandHandler.parse(text, followup=self._awaiting_followup):
			self._awaiting_followup = False
			self.llm.send(text)
			self.wakeword.set_enabled(False)
			dispatcher.send(signal="animationStart", name="thinking", bStartAtRandomTime=False, bLoop=True)
			self.led_controller.set_state(LEDController.STATE_THINKING)
		else:
			# Handled locally. If it plays audio the LEDs are picked up again
			# by on_voice_playback_event; if it plays nothing, this is the end.
			self._awaiting_followup = False
			dispatcher.send(signal="animationStop")
			self.led_controller.set_state(LEDController.STATE_OFF)
			self.wakeword.set_enabled(True)
			self.movements.reset_all()

	# The marker is not always the last thing on the line — small models emit
	# "[?]." and "[?] !" too, which a plain endswith() misses, leaving the
	# marker to be spoken and the follow-up never triggered.
	_TRAILING_MARKER_RE = re.compile(r"\s*\[\s*\?\s*\]\s*[.!?,]*\s*$")

	def on_execute_text_to_speech(self, text: str, bForceOffline: bool = False,
	                              utterance_id: str = None) -> None:
		"""bForceOffline bypasses ElevenLabs and speaks with the Piper voice.
		utterance_id marks speech that came from an LLM response, so its
		completion can be broadcast."""
		match = self._TRAILING_MARKER_RE.search(text)
		if match:
			self._awaiting_followup = True
			text = text[:match.start()].rstrip()
			log.info("Response: {} [?]".format(text))
		else:
			self._awaiting_followup = False
			log.info(f"Response: {text}")
		self.tts.speak(text, bForceOffline, utterance_id)

	def on_voice_play(self, file: str, utterance_id: str = None) -> None:
		if self._show_mode is not None:
			# Voice shares mixer.music with the song, and would puppeteer
			# over the show's movements.
			log.info(f"VoicePlayer: not speaking during a show ('{os.path.basename(file)}').")
			return
		dispatcher.send(signal="animationStart", name="speaking", bStartAtRandomTime=True, bLoop=True)
		self.voice_player.play(file, tag=utterance_id)

	def on_voice_play_sequence(self, fileList) -> None:
		if self._show_mode is not None:
			log.info("VoicePlayer: not speaking during a show.")
			return
		self.voice_player.play_sequence(fileList)

	def on_voice_playback_event(self, bPlaying: bool) -> None:
		# Completion of LLM speech (bCompleted/tag) is PeerConversation's
		# business; it listens to this same signal.
		if self._show_mode is not None:
			# Speech was cut off by a show starting. Only the mic needs
			# putting right; no follow-up, no wakeword, no LED change.
			if not bPlaying:
				mic_stream.set_muted(False)
			return
		if bPlaying:
			mic_stream.set_muted(True)
			self.led_controller.set_state(LEDController.STATE_OFF)
			self.wakeword.set_enabled(False)
		else:
			mic_stream.set_muted(False)
			log.info(f"VoicePlayer: playback ended, _awaiting_followup={self._awaiting_followup}")
			if self._awaiting_followup:
				# Straight back to listening rather than idle.
				self.led_controller.set_state(LEDController.STATE_LISTENING)
				def delayed_listen():
					dispatcher.send(signal="animationStart", name="wakeword")
					mic_stream.set_anchor()
					time.sleep(0.5)
					log.info("Kermit: awaiting follow-up response, listening...")
					self.stt.listen_once()
				threading.Thread(target=delayed_listen, daemon=True).start()
			else:
				dispatcher.send(signal="animationStop")
				self.led_controller.set_state(LEDController.STATE_OFF)
				self.wakeword.set_enabled(True)

	def on_update_status(self, id: str, value: any = None) -> None:
		self._prev_status_id = id
		self._prev_status_value = value
		self.web_server.broadcast('statusUpdate', {"id": id, "value": value})

	def on_web_tts_event(self, val: any) -> None:
		dispatcher.send(signal="voiceInputEvent", id="ttsSubmitted")

	# -------------------------------------------------------------------------
	# WiFi signal handlers
	# -------------------------------------------------------------------------

	def on_connect_to_wifi_network(self, ssid: str, password: any = None) -> None:
		self.wifi_management.connect(ssid, password if password else None)

	def on_wifi_scan_complete(self, networks: list) -> None:
		self.wifi_access_points = networks
		self.web_server.broadcast('wifiScan', networks)
		current_ssid = self.wifi_management.get_current_ssid()
		if current_ssid:
			match = next((n for n in networks if n['ssid'] == current_ssid), None)
			if match:
				self.web_server.broadcast('wifiConnected', {'ssid': current_ssid, 'signal': match['signal_strength']})

	def on_wifi_connected(self, ssid: str) -> None:
		log.info(f"WiFi connected: {ssid}")
		signal_strength = 0
		if self.wifi_access_points:
			match = next((n for n in self.wifi_access_points if n['ssid'] == ssid), None)
			if match:
				signal_strength = match['signal_strength']
		self.web_server.broadcast('wifiConnected', {'ssid': ssid, 'signal': signal_strength})

if __name__ == "__main__":
	animatronic = Kermit()
	animatronic.run()