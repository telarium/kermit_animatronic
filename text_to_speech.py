#!/usr/bin/env python3
import configparser
import threading
import tempfile
import os
import requests
from pydispatch import dispatcher

ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1/text-to-speech"


class TextToSpeech:
	def __init__(self) -> None:
		self.elevenlabs_key: str = ""
		self.elevenlabs_voice_id: str = ""
		print("Set up TextToSpeech")

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

	def apply_config(self, path: str) -> None:
		config = configparser.ConfigParser()
		try:
			config.read(path)
		except configparser.Error as e:
			print(f"TextToSpeech: failed to parse config at '{path}': {e}")
			return

		self.elevenlabs_key      = config.get("TextToSpeech", "ElevenLabsKey",     fallback="").strip()
		self.elevenlabs_voice_id = config.get("TextToSpeech", "ElevenLabsVoiceID", fallback="").strip()

	def speak(self, text: str) -> None:
		"""Convert text to speech asynchronously. Downloads audio to a temp file."""
		threading.Thread(target=self._speak, args=(text,), daemon=True).start()

	# -------------------------------------------------------------------------
	# Internal
	# -------------------------------------------------------------------------

	def _speak(self, text: str) -> None:
		if not self.elevenlabs_key:
			print("TextToSpeech: no ElevenLabs API key set.")
			return
		if not self.elevenlabs_voice_id:
			print("TextToSpeech: no ElevenLabs voice ID set.")
			return

		try:
			response = requests.post(
				f"{ELEVENLABS_API_URL}/{self.elevenlabs_voice_id}",
				headers={
					"xi-api-key": self.elevenlabs_key,
					"Content-Type": "application/json",
				},
				json={
					"text": text,
					"model_id": "eleven_turbo_v2_5",
					"voice_settings": {
						"stability": 0.5,
						"similarity_boost": 0.75,
					},
				},
				timeout=30,
			)
			response.raise_for_status()

			tmp = tempfile.NamedTemporaryFile(
				suffix=".mp3", delete=False, prefix="kermit_tts_"
			)
			tmp.write(response.content)
			tmp.close()

			dispatcher.send(signal="ttsEvent", file=tmp.name)

			print(f"TextToSpeech: audio saved to {tmp.name}")

		except requests.HTTPError as e:
			print(f"TextToSpeech: HTTP error from ElevenLabs: {e}")
		except Exception as e:
			print(f"TextToSpeech: request failed: {e}")
