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
			print(f"Audio: mixer init failed (attempt {attempt + 1}/30): {e}, retrying...")
	else:
		print(f"Audio: APE device not found (attempt {attempt + 1}/30), retrying...")
	time.sleep(2)
else:
	print("Audio: APE audio device not found after 30 attempts. Exiting.")
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
from show_player import ShowPlayer
from wifi_management import WifiManagement

# Restore stderr now that all noisy imports are done
os.dup2(_old_stderr, 2)
os.close(_old_stderr)
_devnull.close()

print("Startup complete.")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_hardware_config(config_path: str) -> dict:
	"""Read the hardware JSON path from config.cfg, load and return its contents. Exits if missing or not found."""
	cfg = configparser.ConfigParser()
	cfg.read(config_path)
	json_path = cfg.get("Hardware", "config", fallback="").strip()
	if not json_path:
		print("Error: 'config' not specified under [Hardware] in config.cfg. Cannot proceed.", file=sys.stderr)
		sys.exit(1)
	abs_path = os.path.join(_BASE_DIR, json_path)
	if not os.path.exists(abs_path):
		print(f"Error: Hardware config not found at '{abs_path}'. Cannot proceed.", file=sys.stderr)
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

		# Resolve where the config and shows live (USB-first, bootstrapping
		# from the template if needed) before anything reads them. The
		# hardware JSON path comes from the config, so startup cannot
		# proceed without one.
		self.config_path, self.shows_dir, _ = utils.resolve_storage(
			_BASE_DIR, usb_monitor.USB_MOUNT_POINT, usb_monitor.is_mounted()
		)
		if not self.config_path:
			print("Error: No usable config.cfg found and none could be created from config_template.cfg. Cannot proceed.", file=sys.stderr)
			sys.exit(1)
		hardware = _load_hardware_config(self.config_path)
		self._key_map = self._build_key_map(hardware)

		wakeword_model  = os.path.join(_BASE_DIR, hardware['wakeword']['model'])
		wakeword_desc   = hardware['wakeword']['description']
		voices_dir      = os.path.join(_BASE_DIR, hardware['voice_directory'])
		animation_dir   = os.path.join(_BASE_DIR, hardware.get('animation_directory', ''))
		hardware_path   = hardware['_path']
		html_config     = hardware.get('html', {})

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
		self.voiceCommandHandler = VoiceCommandHandler(self.wifi_management, self.show_player)

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

		if not resolved:
			self.config_path = None
			print("Warning: No usable config found and none could be created. Continuing with no config.")
			return

		self.config_path = resolved
		print(f"Config loaded from {resolved} ({'USB' if using_usb else 'local backup'})")
		if apply_wifi:
			self.wifi_management.apply_config(resolved)
		self.llm.apply_config(resolved)
		self.tts.apply_config(resolved)
		self.wakeword.apply_config(resolved)
		self._config_data = self._build_config_data(resolved)
		self.web_server.broadcast('configLoaded', self._config_data)

	# Sections excluded from the web broadcast — WiFi already has its own
	# dedicated UI (scan list + connect popup), and Hardware (the character
	# JSON path) isn't something to edit from the web page.
	BROADCAST_EXCLUDED_SECTIONS = ("wifi", "hardware")

	def _build_config_data(self, path: str) -> dict:
		"""Parse the config file into {section: {key: value}} for the web UI."""
		return utils.build_config_data(path, self.BROADCAST_EXCLUDED_SECTIONS)

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
			print(f"Config: save failed: {e}")
			self.web_server.broadcast('configSaveResult', {'success': False, 'error': str(e)})
			return

		print(f"Config: saved {sum(len(v) for v in updates.values() if isinstance(v, dict))} value(s) to {self.config_path}")

		# Mirror the saved config to the other location(s): local dir + USB.
		sync_errors = utils.sync_config_copies(
			self.config_path, _BASE_DIR, usb_monitor.USB_MOUNT_POINT, usb_monitor.is_mounted()
		)
		for err in sync_errors:
			print(f"Config: {err}")

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
		print("Config: USB drive removed, falling back to the local backup.")
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
				print(f"Restore: {message}")
			self.web_server.broadcast('restoreBackupResult',
				{'success': success, 'message': message})
		threading.Thread(target=restore, daemon=True).start()

	def run(self) -> None:
		try:
			while self.is_running:
				time.sleep(0.005)
		except Exception as e:
			print(f"Error in main loop: {e}")
		finally:
			print("Main loop exiting, calling shutdown...")
			self.shutdown()

	def shutdown(self, *args) -> None:
		try:
			self.is_running = False

			if self.web_server:
				self.web_server.shutdown()

			if self.show_player:
				self.show_player.stop_show()

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
							print(f"Error stopping thread {thread.name}: {e}")

			pygame.mixer.quit()
			pygame.display.quit()
			pygame.quit()

			print("Shutdown complete. Exiting.")
			sys.exit(0)

		except Exception as e:
			print(f"Error during shutdown: {e}")
			sys.exit(1)

	def on_show_list_load(self, show_list: any) -> None:
		self.web_server.broadcast('showListLoaded', show_list)

	def on_show_status(self, status: str, show_name: str = "") -> None:
		self._prev_show_status = status
		self.web_server.broadcast('showStatusUpdated', status)
		if status == "play":
			self.show_player.load_show(show_name)
			self.wakeword.set_enabled(False)
		elif status == "pause":
			self.show_player.toggle_pause()
			self.wakeword.set_enabled(True)
		elif status == "stop":
			self.show_player.stop_show()
			self.movements.reset_all()
			self.wakeword.set_enabled(True)
		elif status == "end":
			self.wakeword.set_enabled(True)
			self.movements.reset_all()

	def on_connect_event(self, client_ip: str) -> None:
		print(f"Web client connected from IP: {client_ip}")
		self.web_server.broadcast('voiceCommandUpdate', {"id": "idle", "value": ""})
		self.show_player.get_show_list()
		self.web_server.broadcast('wifiScan', self.wifi_access_points)
		self.web_server.broadcast('showStatusUpdated', self._prev_show_status)
		self.web_server.broadcast('configLoaded', self._config_data)
		self.web_server.broadcast('keyMapLoaded', self._key_map)
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
		self.led_controller.set_state(LEDController.STATE_LISTENING)
		def handle():
			self.show_player.stop_show()
			audio_setup.wake_dac_if_needed(pygame)
			dispatcher.send(signal="animationStart", name="wakeword")
			if not self.wakeword.wait_until_stopped(timeout=4.0):
				print("WakeWord: timed out waiting for listen loop to exit, proceeding anyway.")
			self.stt.listen_once()
		threading.Thread(target=handle, daemon=True).start()

	def on_transcription_result(self, text: str) -> None:
		if not text or text == "[SILENCE]":
			if self._awaiting_followup:
				self._awaiting_followup = False
			self.wakeword.set_enabled(True)
			dispatcher.send(signal="animationStop")
			self.led_controller.set_state(LEDController.STATE_OFF)
			self.movements.reset_all()
			return
		print(f"Heard: {text}")
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

	def on_execute_text_to_speech(self, text: str, bForceOffline: bool = False) -> None:
		"""bForceOffline bypasses ElevenLabs and speaks with the Piper voice."""
		match = self._TRAILING_MARKER_RE.search(text)
		if match:
			self._awaiting_followup = True
			text = text[:match.start()].rstrip()
			print("Response: {} [?]".format(text))
		else:
			self._awaiting_followup = False
			print(f"Response: {text}")
		self.tts.speak(text, bForceOffline)

	def on_voice_play(self, file: str) -> None:
		dispatcher.send(signal="animationStart", name="speaking", bStartAtRandomTime=True, bLoop=True)
		self.voice_player.play(file)

	def on_voice_play_sequence(self, fileList) -> None:
		self.voice_player.play_sequence(fileList)

	def on_voice_playback_event(self, bPlaying: bool) -> None:
		if bPlaying:
			mic_stream.set_muted(True)
			self.led_controller.set_state(LEDController.STATE_OFF)
			self.wakeword.set_enabled(False)
		else:
			mic_stream.set_muted(False)
			print(f"VoicePlayer: playback ended, _awaiting_followup={self._awaiting_followup}")
			if self._awaiting_followup:
				# Straight back to listening rather than idle.
				self.led_controller.set_state(LEDController.STATE_LISTENING)
				def delayed_listen():
					dispatcher.send(signal="animationStart", name="wakeword")
					mic_stream.set_anchor()
					time.sleep(0.5)
					print("Kermit: awaiting follow-up response, listening...")
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
		print(f"WiFi connected: {ssid}")
		signal_strength = 0
		if self.wifi_access_points:
			match = next((n for n in self.wifi_access_points if n['ssid'] == ssid), None)
			if match:
				signal_strength = match['signal_strength']
		self.web_server.broadcast('wifiConnected', {'ssid': ssid, 'signal': signal_strength})

if __name__ == "__main__":
	animatronic = Kermit()
	animatronic.run()