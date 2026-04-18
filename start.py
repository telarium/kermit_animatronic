#!/usr/bin/env python3
import os
import sys
import warnings

# Suppress noise — must be before any imports that touch audio
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "1"
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['SDL_AUDIODRIVER'] = 'dummy'  # No audio output device yet
os.environ["PYTHONWARNINGS"] = "ignore"
os.environ["ORT_LOGGING_LEVEL"] = "3"
if 'XDG_RUNTIME_DIR' not in os.environ:
	os.environ['XDG_RUNTIME_DIR'] = "/tmp"
warnings.filterwarnings("ignore")

# Suppress all stderr noise (ALSA, onnxruntime, pyaudio) during startup
_devnull = open(os.devnull, 'w')
_old_stderr = os.dup(2)
os.dup2(_devnull.fileno(), 2)

# Init pygame mixer FIRST before anything else touches ALSA
import pygame
pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=2048)
pygame.mixer.init()

# Now safe to import everything else
import signal
import time
import threading
import ctypes
from pydispatch import dispatcher
from web_io import WebServer
from gpio import GPIO
from wakeword_detection import WakeWord
from speech_to_text import SpeechToText
from voice_commands import VoiceCommandHandler
from animatronic_movements import Movement
from gamepad_input import USBGamepadReader
from show_player import ShowPlayer
from wifi_management import WifiManagement

# Restore stderr now that all noisy imports are done
os.dup2(_old_stderr, 2)
os.close(_old_stderr)
_devnull.close()

print("Startup complete.")

class Kermit:
	def __init__(self) -> None:
		self.is_running: bool = True
		self.wifi_access_points = None

		# Initialize components
		self.gpio = GPIO()
		self.wakeword = WakeWord()
		self.stt = SpeechToText()
		self.voiceCommandHandler = VoiceCommandHandler()
		self.movements = Movement(self.gpio)
		self.web_server = WebServer()
		self.wifi_management = WifiManagement()
		self.gamepad = USBGamepadReader(self.movements, self.web_server)
		self.show_player = ShowPlayer(pygame)

		self.set_dispatch_events()

		# Handle SIGINT and SIGTERM for graceful shutdown
		signal.signal(signal.SIGINT, self.shutdown)
		signal.signal(signal.SIGTERM, self.shutdown)

		self.wakeword.set_enabled(True)
		self.movements.set_default_animation(True)
		self.wifi_management.scan()

	def set_dispatch_events(self) -> None:
		dispatcher.connect(self.on_key_event, signal='keyEvent', sender=dispatcher.Any)
		dispatcher.connect(self.on_mirrored_mode_toggle, signal='mirrorModeToggle', sender=dispatcher.Any)
		dispatcher.connect(self.on_connect_event, signal='connectEvent', sender=dispatcher.Any)
		dispatcher.connect(self.on_wakeword_event, signal='wakewordEvent', sender=dispatcher.Any)
		dispatcher.connect(self.on_transcription_result, signal='transcriptionResult', sender=dispatcher.Any)
		dispatcher.connect(self.on_show_list_load, signal='showListLoad', sender=dispatcher.Any)
		dispatcher.connect(self.on_show_play, signal='showPlay', sender=dispatcher.Any)
		dispatcher.connect(self.on_show_pause, signal='showPause', sender=dispatcher.Any)
		dispatcher.connect(self.on_show_stop, signal='showStop', sender=dispatcher.Any)
		dispatcher.connect(self.on_show_end, signal='showEnd', sender=dispatcher.Any)
		dispatcher.connect(self.on_mirrored_mode, signal='onMirroredMode', sender=dispatcher.Any)
		dispatcher.connect(self.on_show_playback_midi_event, signal='showPlaybackMidiEvent', sender=dispatcher.Any)
		dispatcher.connect(self.on_connect_to_wifi_network, signal='connectToWifi', sender=dispatcher.Any)
		dispatcher.connect(self.on_web_tts_event, signal='webTTSEvent', sender=dispatcher.Any)
		# WiFi signals from WifiManagement
		dispatcher.connect(self.on_wifi_scan_complete, signal='wifiScanComplete', sender=dispatcher.Any)
		dispatcher.connect(self.on_wifi_connected, signal='wifiConnected', sender=dispatcher.Any)
		dispatcher.connect(self.on_wifi_password_required, signal='wifiPasswordRequired', sender=dispatcher.Any)
		dispatcher.connect(self.on_wifi_wrong_password, signal='wifiWrongPassword', sender=dispatcher.Any)
		dispatcher.connect(self.on_wifi_disconnected, signal='wifiDisconnected', sender=dispatcher.Any)

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
			self.is_running = False  # Signal all loops to stop

			# Stop all dependent components
			if self.web_server:
				self.web_server.shutdown()

			if self.show_player:
				self.show_player.stop_show()

			if self.stt:
				self.stt.shutdown()

			# Ensure all non-main threads exit before quitting pygame
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

	def on_show_play(self, show_name: str) -> None:
		self.wakeword.set_enabled(False)
		self.show_player.load_show(show_name)
		self.movements.set_default_animation(False)

	def on_show_stop(self) -> None:
		self.wakeword.set_enabled(True)
		self.show_player.stop_show()
		self.movements.set_default_animation(True)

	def on_show_end(self) -> None:
		self.wakeword.set_enabled(True)
		self.movements.set_default_animation(True)

	def on_show_pause(self) -> None:
		self.wakeword.set_enabled(True)
		self.show_player.toggle_pause()

	def on_show_playback_midi_event(self, midi_note: any, val: any) -> None:
		self.movements.execute_midi_note(midi_note, val)

	def on_connect_event(self, client_ip: str) -> None:
		print(f"Web client connected from IP: {client_ip}")
		self.show_player.get_show_list()
		self.web_server.broadcast('movementInfo', self.movements.get_all_movement_info())
		# Send the cached scan results and current wifi status to the newly connected client, then kick off a fresh scan
		self.web_server.broadcast('wifiScan', self.wifi_access_points)
		current_ssid = self.wifi_management.get_current_ssid()
		if current_ssid:
			match = next((n for n in (self.wifi_access_points or []) if n['ssid'] == current_ssid), None)
			self.web_server.broadcast('wifiConnected', {'ssid': current_ssid, 'signal': match['signal_strength'] if match else 0})
		self.wifi_management.scan()

	def on_wakeword_event(self) -> None:
		def handle():
			self.wakeword.set_enabled(False)
			self.stt.listen_once()
		threading.Thread(target=handle, daemon=True).start()

	def on_transcription_result(self, text: str) -> None:
		print(f"Heard: {text}")
		# TODO: send to LLM
		if( not self.voiceCommandHandler.parse(text)):
			pring("TODO: LLM MAGIC!")

		self.wakeword.set_enabled(True)

	def on_key_event(self, key: any, val: any) -> None:
		try:
			self.movements.execute_movement(str(key).lower(), val)
		except Exception as e:
			print(f"Invalid key: {e}")

	def on_mirrored_mode(self, val: any) -> None:
		self.movements.set_mirrored(val)

	def on_mirrored_mode_toggle(self) -> None:
		new_mirror_mode = not self.movements.b_mirrored
		self.movements.set_mirrored(new_mirror_mode)

	def on_connect_to_wifi_network(self, ssid: str, password: any = None) -> None:
		self.wifi_management.connect(ssid, password if password else None)

	def on_web_tts_event(self, val: any) -> None:
		dispatcher.send(signal="voiceInputEvent", id="ttsSubmitted")
		print("TODO TTS EVENT")

	# -------------------------------------------------------------------------
	# WiFi signal handlers
	# -------------------------------------------------------------------------

	def on_wifi_scan_complete(self, networks: list) -> None:
		self.wifi_access_points = networks
		self.web_server.broadcast('wifiScan', networks)
		# Update the wifi status link with fresh signal strength now that we have scan data
		current_ssid = self.wifi_management.get_current_ssid()
		if current_ssid:
			match = next((n for n in networks if n['ssid'] == current_ssid), None)
			if match:
				self.web_server.broadcast('wifiConnected', {'ssid': current_ssid, 'signal': match['signal_strength']})

	def on_wifi_connected(self, ssid: str) -> None:
		print(f"WiFi connected: {ssid}")
		# Pull signal from the cached scan rather than querying the radio,
		# which may not have settled yet right after connecting.
		signal_strength = 0
		if self.wifi_access_points:
			match = next((n for n in self.wifi_access_points if n['ssid'] == ssid), None)
			if match:
				signal_strength = match['signal_strength']
		self.web_server.broadcast('wifiConnected', {'ssid': ssid, 'signal': signal_strength})

	def on_wifi_password_required(self, ssid: str) -> None:
		print(f"WiFi password required for: {ssid}")
		self.web_server.broadcast('wifiPasswordRequired', {'ssid': ssid})

	def on_wifi_wrong_password(self, ssid: str) -> None:
		print(f"WiFi wrong password for: {ssid}")
		self.web_server.broadcast('wifiWrongPassword', {'ssid': ssid})

	def on_wifi_disconnected(self) -> None:
		print("WiFi disconnected.")
		self.web_server.broadcast('wifiDisconnected', {})


if __name__ == "__main__":
	animatronic = Kermit()
	animatronic.run()
