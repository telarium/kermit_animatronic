#!/usr/bin/env python3
from rapidfuzz import process, fuzz
from pydispatch import dispatcher

class VoiceCommandHandler:

	INTENTS = {
		"look_left": [
			"look left", "turn left", "move left", "face left", "go left", "rotate left",
		],
		"look_right": [
			"look right", "turn right", "move right", "face right", "go right", "rotate right",
		],
		"look_up": [
			"look up", "turn up", "move up", "face up", "go up", "look upward", "glance up",
		],
		"look_down": [
			"look down", "turn down", "move down", "face down", "go down", "look downward", "glance down",
		],
		"sing": [
			"sing a song", "sing for me", "play something", "play music", "sing music",
			"entertain me", "hit it", "sing something", "play something for me",
			"give me a song", "do a song", "let's hear some music",
		],
		"get_ip": [
			"what's your wifi address", "what is your wifi address",
			"what's your ip address", "what is your ip address",
			"what's your ip", "tell me your ip", "ip address please",
			"give me your ip", "your ip address", "network address",
		],
		"get_wifi_network": [
			"what's your wifi network", "what is your wifi network",
			"what wifi are you connected to", "what network are you on",
			"which wifi are you on", "are you connected to wifi",
			"what's your network", "tell me your wifi", "which network",
		],
		"who_are_you": [
			"who are you", "what's your name", "tell me about you", "tell me about yourself",
			"are you a robot", "are you ai", "are you artificial intelligence",
			"what are you", "introduce yourself", "who are you exactly",
			"are you real", "are you alive",
		],
		"greeting": [
			"hello", "hi", "hey", "greetings", "what's up", "whats up",
			"good morning", "good afternoon", "good evening", "howdy",
			"hey there", "hi there", "how are you", "how's it going",
			"what is up", "sup",
		],
	}

	PLAY_BY_NAME_PREFIXES = [
		"i want to sing", "can i hear", "i want to hear", "let's hear",
		"let's sing", "could you play", "can you play", "can you sing",
		"please play", "please sing", "i'd like to hear", "play the song",
		"sing the song", "play", "sing",
	]

	# Generic words that should NOT be treated as song names
	PLAY_BY_NAME_BLOCKLIST = {
		"something", "music", "a song", "for me", "anything", "a tune",
		"something for me", "me a song", "us a song",
	}

	CONFIDENCE_THRESHOLD = 80

	def __init__(self) -> None:
		self._phrase_map = [
			(phrase, intent_name)
			for intent_name, phrases in self.INTENTS.items()
			for phrase in phrases
		]
		# Sort prefixes longest-first so more specific ones match before shorter ones
		self._sorted_prefixes = sorted(self.PLAY_BY_NAME_PREFIXES, key=len, reverse=True)
		print("VoiceCommandHandler: initialized.")

	def parse(self, transcript: str) -> bool:
		"""
		Attempt to detect a command in the transcript.
		Returns True if a confident match was found and handled, False otherwise.
		"""
		text = transcript.lower().strip()

		# 1. Check play-by-name first (e.g. "play Rainbow Connection")
		song_name = self._match_play_by_name(text)
		if song_name is not None:
			self._handle_play_by_name(song_name)
			return True

		# 2. Fuzzy match against all intent phrases
		phrase_strings = [p[0] for p in self._phrase_map]
		match = process.extractOne(text, phrase_strings, scorer=fuzz.ratio)
		if match and match[1] >= self.CONFIDENCE_THRESHOLD:
			intent_name = next(name for phrase, name in self._phrase_map if phrase == match[0])
			print(f"VoiceCommandHandler: matched intent='{intent_name}' phrase='{match[0]}' score={match[1]}")
			self._dispatch_intent(intent_name)
			return True

		print(f"VoiceCommandHandler: no confident match for '{transcript}' (best score={match[1] if match else 0})")
		return False

	def _match_play_by_name(self, text: str) -> str | None:
		"""
		If the transcript starts with a play-by-name prefix and has a meaningful
		remainder, return the remainder as the song name. Otherwise return None.
		"""
		for prefix in self._sorted_prefixes:
			if text.startswith(prefix + " ") or text == prefix:
				remainder = text[len(prefix):].strip()
				if remainder and remainder not in self.PLAY_BY_NAME_BLOCKLIST:
					return remainder
		return None

	def _dispatch_intent(self, intent: str) -> None:
		handlers = {
			"look_left":        self._handle_look_left,
			"look_right":       self._handle_look_right,
			"look_up":          self._handle_look_up,
			"look_down":        self._handle_look_down,
			"sing":             self._handle_sing,
			"get_ip":           self._handle_get_ip,
			"get_wifi_network": self._handle_get_wifi_network,
			"who_are_you":      self._handle_who_are_you,
			"greeting":         self._handle_greeting,
		}
		handler = handlers.get(intent)
		if handler:
			dispatcher.send(signal="updateStatus", id="command", value=intent)
			handler()

	# --- intent handlers ---

	def _handle_look_left(self) -> None:
		print("VoiceCommandHandler: look left")
		# TODO: dispatcher.send(signal='movementCommand', movement='look_left')

	def _handle_look_right(self) -> None:
		print("VoiceCommandHandler: look right")
		# TODO: dispatcher.send(signal='movementCommand', movement='look_right')

	def _handle_look_up(self) -> None:
		print("VoiceCommandHandler: look up")
		# TODO: dispatcher.send(signal='movementCommand', movement='look_up')

	def _handle_look_down(self) -> None:
		print("VoiceCommandHandler: look down")
		# TODO: dispatcher.send(signal='movementCommand', movement='look_down')

	def _handle_sing(self) -> None:
		print("VoiceCommandHandler: sing")
		# TODO: pick a random show and dispatch showPlay

	def _handle_play_by_name(self, song_name: str) -> None:
		print(f"VoiceCommandHandler: play by name — '{song_name}'")
		# TODO: pass song_name to show resolver class

	def _handle_get_ip(self) -> None:
		import socket
		try:
			ip = socket.gethostbyname(socket.gethostname())
		except Exception:
			ip = "unknown"
		print(f"VoiceCommandHandler: IP address is {ip}")
		# TODO: dispatcher.send(signal='ttsSpeak', text=f"My IP address is {ip}")

	def _handle_get_wifi_network(self) -> None:
		import subprocess
		try:
			result = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True)
			ssid = result.stdout.strip() or "unknown"
		except Exception:
			ssid = "unknown"
		print(f"VoiceCommandHandler: wifi network is {ssid}")
		# TODO: dispatcher.send(signal='ttsSpeak', text=f"I'm connected to {ssid}")

	def _handle_who_are_you(self) -> None:
		print("VoiceCommandHandler: who are you")
		# TODO: dispatcher.send(signal='ttsSpeak', text="I'm Kermit the Frog!")

	def _handle_greeting(self) -> None:
		print("VoiceCommandHandler: greeting")
		# TODO: dispatcher.send(signal='ttsSpeak', text="Well hello there!")