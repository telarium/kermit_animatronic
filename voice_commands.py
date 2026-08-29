#!/usr/bin/env python3
import random
import re
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
			"ip address", "ip", "which are IP address",
		],
		"get_wifi_network": [
			"what's your wifi network", "what is your wifi network",
			"what wifi are you connected to", "what network are you on",
			"which wifi are you on", "are you connected to wifi",
			"what's your network", "tell me your wifi", "which network", "wifi network"
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

	CONNECT_WIFI_PREFIXES = [
		"connect to network", "connect to wifi", "log in to wifi",
		"log in to network", "connect to", "log in to",
	]

	# Generic words that should NOT be treated as song names
	PLAY_BY_NAME_BLOCKLIST = {
		"something", "music", "a song", "for me", "anything", "a tune",
		"something for me", "me a song", "us a song",
	}

	CONFIDENCE_THRESHOLD = 80

	# Song titles are matched with a separate, lower threshold. Show names come
	# from filename stems that carry material the speaker never says (artist,
	# track number), so scores run lower than for the fixed intent phrases
	# above even when the match is obviously correct.
	SHOW_CONFIDENCE_THRESHOLD = 72

	# "Artist - Title" / "Title - Artist" separator. Matches a hyphen, en dash
	# or em dash that is surrounded by whitespace, so hyphenated words inside a
	# title ("Spider-Man") are left intact.
	_ARTIST_SPLIT = re.compile(r"\s+[-–—]\s+")

	# Leading track numbers: "01 - Title", "03. Title", "7 Title".
	_TRACK_PREFIX = re.compile(r"^\s*\d{1,3}\s*[-–—._)]*\s+")

	# Filler the speaker adds that never appears in a filename.
	_QUERY_FILLER = re.compile(r"^(the |a |some )?(song|tune|track) (called |named )?")

	@staticmethod
	def _normalize(text: str) -> str:
		"""Fold a title or query into comparable form.

		Speech-to-text writes what was said ("and"), filenames write what was
		typed ("&"). Punctuation, casing and apostrophes differ freely between
		the two and carry no meaning for matching, so strip all of it.
		"""
		text = text.lower()
		text = text.replace("&", " and ").replace("+", " and ")
		text = re.sub(r"[^a-z0-9]+", " ", text)
		return re.sub(r"\s+", " ", text).strip()

	@classmethod
	def _show_keys(cls, show_name: str) -> set:
		"""Every normalized string that should match this show.

		A stem like "Gin & Juice - Snoop Frogg" yields the whole thing plus
		each side of the separator, so "gin and juice" scores against the
		title alone instead of being diluted by an artist name the speaker
		never said. Both sides are indexed because filenames use both
		"Artist - Title" and "Title - Artist" conventions and we can't tell
		which from the name.
		"""
		stem = cls._TRACK_PREFIX.sub("", show_name)
		keys = {cls._normalize(stem)}
		parts = cls._ARTIST_SPLIT.split(stem)
		if len(parts) > 1:
			keys.update(cls._normalize(p) for p in parts)
		return {k for k in keys if k}

	def __init__(self, wifi_management, show_player) -> None:
		self._wifi_management = wifi_management
		self._show_player = show_player
		self._phrase_map = [
			(phrase, intent_name)
			for intent_name, phrases in self.INTENTS.items()
			for phrase in phrases
		]
		self._sorted_prefixes = sorted(self.PLAY_BY_NAME_PREFIXES, key=len, reverse=True)
		self._sorted_connect_prefixes = sorted(self.CONNECT_WIFI_PREFIXES, key=len, reverse=True)
		# Lazily built index of (normalized key -> original show stem). Rebuilt
		# whenever show_list changes, since inserting a USB stick adds shows at
		# runtime. _resolve_show needs the ORIGINAL stem back to find the file,
		# so the index maps to it rather than to the normalized form.
		self._show_index: list = []
		self._show_index_source: tuple = ()
		self._normalized_blocklist = {
			self._normalize(p) for p in self.PLAY_BY_NAME_BLOCKLIST
		}
		print("VoiceCommandHandler: initialized.")

	def parse(self, transcript: str) -> bool:
		"""
		Attempt to detect a command in the transcript.
		Returns True if a confident match was found and handled, False otherwise.
		"""
		text = transcript.lower().strip().rstrip(".,!?")

		# 1. Check play-by-name first (e.g. "play Rainbow Connection")
		song_name = self._match_play_by_name(text)
		if song_name is not None:
			if self._handle_play_by_name(song_name):
				return True

		# 2. Check connect-to-wifi prefix (e.g. "connect to MyNetwork")
		ssid_name = self._match_connect_wifi(text)
		if ssid_name is not None:
			self._handle_connect_wifi(ssid_name)
			return True

		# 3. Fuzzy match against all intent phrases
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
				# Compare against the blocklist normalized, so trailing
				# punctuation from the transcript ("play something.") doesn't
				# sneak a generic phrase through as a song title.
				if remainder and self._normalize(remainder) not in self._normalized_blocklist:
					return remainder
		return None

	def _match_connect_wifi(self, text: str) -> str | None:
		"""
		If the transcript starts with a connect-wifi prefix and has a meaningful
		remainder, return the remainder as the SSID. Otherwise return None.
		"""
		for prefix in self._sorted_connect_prefixes:
			if text.startswith(prefix + " ") or text == prefix:
				remainder = text[len(prefix):].strip()
				if remainder:
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
			dispatcher.send(signal="updateStatus", id="Command", value=intent)
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
		show_list = self._show_player.show_list
		if not show_list:
			print("VoiceCommandHandler: no shows available.")
			return
		show_name = random.choice(show_list)
		print(f"VoiceCommandHandler: randomly selected show '{show_name}'")
		dispatcher.send(signal='showStatus', status='play', show_name=show_name)

	def _get_show_index(self, show_list: list) -> list:
		"""Return [(normalized_key, show_stem), ...], rebuilding if stale."""
		source = tuple(show_list)
		if source != self._show_index_source:
			index = []
			for show in show_list:
				for key in self._show_keys(show):
					index.append((key, show))
			self._show_index = index
			self._show_index_source = source
			print(f"VoiceCommandHandler: indexed {len(show_list)} shows "
			      f"as {len(index)} match keys.")
		return self._show_index

	def _handle_play_by_name(self, song_name: str) -> bool:
		"""
		Fuzzy-match song_name against the available show list.
		Dispatches showStatus if confident. Returns True if a match was found.
		"""
		show_list = self._show_player.show_list
		if not show_list:
			print("VoiceCommandHandler: no shows available.")
			return False

		query = self._normalize(self._QUERY_FILLER.sub("", song_name))
		if not query:
			return False

		index = self._get_show_index(show_list)
		keys = [k for k, _ in index]

		# WRatio rather than ratio: it also considers partial and token-set
		# similarity, and applies a length penalty so a single shared word out
		# of a long title doesn't score as a full match. Plain ratio compares
		# whole strings, which is why "gin and juice" lost against the stem
		# "Gin & Juice - Snoop Frogg" — the artist half it never heard counted
		# against it.
		results = process.extract(query, keys, scorer=fuzz.WRatio, limit=5)
		if not results:
			return False

		# Collapse to the best score per show — one show contributes several
		# keys, and we don't want the same title occupying every slot.
		best_per_show: dict = {}
		for key, score, idx in results:
			show = index[idx][1]
			if score > best_per_show.get(show, (0, ""))[0]:
				best_per_show[show] = (score, key)

		ranked = sorted(best_per_show.items(), key=lambda kv: kv[1][0], reverse=True)
		matched_show, (best_score, matched_key) = ranked[0]

		if best_score < self.SHOW_CONFIDENCE_THRESHOLD:
			print(f"VoiceCommandHandler: play by name — no confident match for "
			      f"'{song_name}' (normalized '{query}', best='{matched_key}' "
			      f"score={best_score:.1f})")
			return False

		# Log the runner-up too. A near-tie means two titles share vocabulary,
		# which is the failure mode worth seeing in the logs before a guest
		# hears the wrong song.
		if len(ranked) > 1:
			runner_up, (runner_score, _) = ranked[1]
			print(f"VoiceCommandHandler: play by name — '{song_name}' matched "
			      f"'{matched_show}' via '{matched_key}' (score={best_score:.1f}; "
			      f"next best '{runner_up}' at {runner_score:.1f})")
		else:
			print(f"VoiceCommandHandler: play by name — '{song_name}' matched "
			      f"'{matched_show}' via '{matched_key}' (score={best_score:.1f})")

		dispatcher.send(signal='showStatus', status='play', show_name=matched_show)
		return True

	def _handle_connect_wifi(self, ssid: str) -> None:
		print(f"VoiceCommandHandler: connect to wifi requested for '{ssid}'")
		dispatcher.send(signal="playVoiceFile", file="connect_to_wifi.ogg")
		self._wifi_management.connect(ssid)

	def _handle_get_ip(self) -> None:
		try:
			ip = self._wifi_management.get_ip()
		except Exception:
			ip = "0"

		if ip == "0":
			dispatcher.send(signal="playVoiceFile", file="no_connection.ogg")
			return

		EXACT_FILES = {
			10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
			30, 40, 50, 60, 70, 80, 90, 100
		}

		files = ["ip_prefix.ogg"]
		for i, octet in enumerate(ip.split(".")):
			n = int(octet)
			if n in EXACT_FILES:
				files.append(f"number_{n}.wav")
			else:
				for digit in octet:
					files.append(f"number_{digit}.wav")
			if i < 3:
				files.append("dot.wav")

		dispatcher.send(signal="playVoiceSequence", fileList=files)

	def _handle_get_wifi_network(self) -> None:
		ssid = self._wifi_management.get_current_ssid()
		if ssid:
			responses = [
				f"Well - currently I'm connected to {ssid}",
				f"Right now I'm connected to {ssid}",
				f"Oh, I'm hooked up to {ssid}",
				f"My wifi network is {ssid}",
			]
			dispatcher.send(signal="executeTTS", text=random.choice(responses))
		else:
			dispatcher.send(signal="playVoiceFile", file="no_connection.ogg")

	def _handle_who_are_you(self) -> None:
		dispatcher.send(signal="playVoiceFile", file="who_are_you.ogg")

	def _handle_greeting(self) -> None:
		print("VoiceCommandHandler: greeting")
		dispatcher.send(signal="playVoiceFile", file="who_are_you.ogg")