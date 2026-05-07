#!/usr/bin/env python3
import os
import sys
import time
import warnings
import subprocess
import usb_monitor
import pygame

# Suppress noise — must be before any imports that touch audio
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "1"
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['SDL_AUDIODRIVER'] = 'alsa'
os.environ["PYTHONWARNINGS"] = "ignore"
os.environ["ORT_LOGGING_LEVEL"] = "3"
if 'XDG_RUNTIME_DIR' not in os.environ:
	os.environ['XDG_RUNTIME_DIR'] = "/tmp"
warnings.filterwarnings("ignore")

# Suppress all stderr noise (ALSA, onnxruntime, pyaudio) during startup
_devnull = open(os.devnull, 'w')
_old_stderr = os.dup(2)
os.dup2(_devnull.fileno(), 2)

# Init pygame mixer — retry until USB audio device is available
pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=16384)
for attempt in range(30):
	audio_card = usb_monitor.find_usb_audio_card()
	if audio_card:
		os.environ['AUDIODEV'] = audio_card
		try:
			pygame.mixer.init()
			break
		except Exception as e:
			print(f"Audio: mixer init failed (attempt {attempt + 1}/30): {e}, retrying...")
	else:
		print(f"Audio: USB device not found (attempt {attempt + 1}/30), retrying...")
	time.sleep(2)
else:
	print("Audio: USB audio device not found after 30 attempts. Exiting.")
	sys.exit(1)

usb_monitor.init_respeaker()

# Now safe to import everything else
import signal
import threading
import ctypes
from pydispatch import dispatcher
from web_io import WebServer
from wakeword_detection import WakeWord
from speech_to_text import SpeechToText
from text_to_speech import TextToSpeech
from voice_commands import VoiceCommandHandler
from llm_service import LLM
from voice_player import VoicePlayer
from animatronic_movements import Movement
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
		self.wakeword = WakeWord()
		self.stt = SpeechToText()
		self.tts = TextToSpeech()
		self.llm = LLM()
		self.voice_player = VoicePlayer(pygame)
		self.movements = Movement()
		self.web_server = WebServer()
		self.wifi_management = WifiManagement()
		self.voiceCommandHandler = VoiceCommandHandler(self.wifi_management)

		self.show_player = ShowPlayer(pygame)

		self.set_dispatch_events()

		# Handle SIGINT and SIGTERM for graceful shutdown
		signal.signal(signal.SIGINT, self.shutdown)
		signal.signal(signal.SIGTERM, self.shutdown)

		self.wakeword.set_enabled(True)
		self.wifi_management.scan()

		self.load_config()

	def set_dispatch_events(self) -> None:
		dispatcher.connect(self.on_key_event, signal='keyEvent', sender=dispatcher.Any)
		dispatcher.connect(self.on_update_status, signal='updateStatus', sender=dispatcher.Any)
		dispatcher.connect(self.on_mirrored_mode_toggle, signal='mirrorModeToggle', sender=dispatcher.Any)
		dispatcher.connect(self.on_connect_event, signal='connectEvent', sender=dispatcher.Any)
		dispatcher.connect(self.load_config, signal='usbConfigFound', sender=dispatcher.Any)
		dispatcher.connect(self.on_wakeword_event, signal='wakewordEvent', sender=dispatcher.Any)
		dispatcher.connect(self.on_transcription_result, signal='transcriptionResult', sender=dispatcher.Any)
		dispatcher.connect(self.on_execute_text_to_speech, signal='executeTTS', sender=dispatcher.Any)
		dispatcher.connect(self.on_voice_play, signal='playVoiceFile', sender=dispatcher.Any)
		dispatcher.connect(self.on_voice_play_sequence, signal='playVoiceSequence', sender=dispatcher.Any)
		dispatcher.connect(self.on_voice_playback_event, signal='voicePlaybackEvent', sender=dispatcher.Any)
		dispatcher.connect(self.on_show_list_load, signal='showListLoad', sender=dispatcher.Any)
		dispatcher.connect(self.on_show_play, signal='showPlay', sender=dispatcher.Any)
		dispatcher.connect(self.on_show_pause, signal='showPause', sender=dispatcher.Any)
		dispatcher.connect(self.on_show_stop, signal='showStop', sender=dispatcher.Any)
		dispatcher.connect(self.on_show_end, signal='showEnd', sender=dispatcher.Any)
		dispatcher.connect(self.on_mirrored_mode, signal='onMirroredMode', sender=dispatcher.Any)
		dispatcher.connect(self.on_connect_to_wifi_network, signal='connectToWifi', sender=dispatcher.Any)
		dispatcher.connect(self.on_wifi_scan_complete, signal='wifiScanComplete', sender=dispatcher.Any)
		dispatcher.connect(self.on_wifi_connected, signal='wifiConnected', sender=dispatcher.Any)
		dispatcher.connect(self.on_wifi_password_required, signal='wifiPasswordRequired', sender=dispatcher.Any)
		dispatcher.connect(self.on_wifi_wrong_password, signal='wifiWrongPassword', sender=dispatcher.Any)
		dispatcher.connect(self.on_wifi_disconnected, signal='wifiDisconnected', sender=dispatcher.Any)

	def load_config(self, path: str = "") -> None:
		if not path:
			usb_cfg = os.path.join(usb_monitor.USB_MOUNT_POINT, "config.cfg")
			if os.path.exists(usb_cfg):
				path = usb_cfg
			else:
				path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.cfg")

		if os.path.exists(path):
			self.config_path = path
			print(f"Config found at {path}")
			self.wifi_management.apply_config(path)
			self.llm.apply_config(path)
			self.tts.apply_config(path)
			self.wakeword.apply_config(path)
		else:
			self.config_path = None
			print(f"Warning: Config file not found at {path}. Continuing with no config.")

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

	def on_show_stop(self) -> None:
		self.wakeword.set_enabled(True)
		self.show_player.stop_show()

	def on_show_end(self) -> None:
		self.wakeword.set_enabled(True)

	def on_show_pause(self) -> None:
		self.wakeword.set_enabled(True)
		self.show_player.toggle_pause()

	def on_connect_event(self, client_ip: str) -> None:
		print(f"Web client connected from IP: {client_ip}")
		self.web_server.broadcast('voiceCommandUpdate', {"id": "idle", "value": ""})
		self.show_player.get_show_list()
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
		if not text:
			self.wakeword.set_enabled(True)
			return
		print(f"Heard: {text}")
		if not self.voiceCommandHandler.parse(text):
			print("SEND LLM!")
			self.llm.send(text)
			self.wakeword.set_enabled(False)
		else:
			self.wakeword.set_enabled(True)

	def on_execute_text_to_speech(self, text: str) -> None:
		print(f"Response: {text}")
		self.tts.speak(text)

	def on_voice_play(self, file: str) -> None:
		self.voice_player.play(file)

	def on_voice_play_sequence(self, fileList) -> None:
		self.voice_player.play_sequence(fileList)

	def on_voice_playback_event(self, bPlaying: bool) -> None:
		if bPlaying:
			self.wakeword.set_enabled(False)
		else:
			self.wakeword.set_enabled(True)

	def on_key_event(self, key: any, val: any) -> None:
		try:
			self.movements.execute_movement(str(key).lower(), val)
		except Exception as e:
			print(f"Invalid key: {e}")

	def on_update_status(self, id: str, value: any = None) -> None:
		self.web_server.broadcast('statusUpdate', {"id": id, "value": value})

	def on_mirrored_mode(self, val: any) -> None:
		self.movements.set_mirrored(val)

	def on_mirrored_mode_toggle(self) -> None:
		new_mirror_mode = not self.movements.b_mirrored
		self.movements.set_mirrored(new_mirror_mode)

	def on_connect_to_wifi_network(self, ssid: str, password: any = None) -> None:
		self.wifi_management.connect(ssid, password if password else None)

	def on_web_tts_event(self, val: any) -> None:
		dispatcher.send(signal="voiceInputEvent", id="ttsSubmitted")

	# -------------------------------------------------------------------------
	# WiFi signal handlers
	# -------------------------------------------------------------------------

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