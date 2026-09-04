#!/usr/bin/env python3
import random
import re
import subprocess
from datetime import datetime
from typing import Optional
from rapidfuzz import process, fuzz
from pydispatch import dispatcher

class VoiceCommandHandler:

	INTENTS = {
		"sing": [
			"sing a song", "sing for me", "play something", "play music", "sing music",
			"entertain me", "hit it", "sing something", "play something for me",
			"give me a song", "do a song", "let's hear some music", "singh", "clay"
		],
		"stop": [
			"stop", "stop music", "stop please", "stop singing", "be quiet", "that's enough",
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
		"get_time": [
			"what time is it", "what's the time", "what is the time",
			"do you have the time", "tell me the time", "got the time",
			"what time do you have", "the time",
		],
		"get_day": [
			"what day of the week is it", "what day is it", "what day is today",
			"what's today", "what is today", "which day is it",
		],
		"get_month": [
			"what month is it", "what's the month", "what is the month",
			"which month is it", "what month are we in",
		],
		"get_date": [
			"what's the date", "what is the date", "what's today's date", "the date",
			"what is today's date", "tell me the date", "the date today",
		],
		"get_year": [
			"what year is it", "what's the year", "what is the year",
			"which year is it", "what year are we in",
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
		# Not synced here: systemd-timesyncd already does NTP properly and keeps
		# doing it. This only reports what it found, so a wrong clock is
		# visible at startup rather than discovered mid-show.
		synced = self._ntp_synchronized()
		if synced is False:
			print("VoiceCommandHandler: system clock is not NTP-synchronized "
			      f"(reads {datetime.now():%Y-%m-%d %H:%M}).")
		elif synced is None:
			print("VoiceCommandHandler: could not determine clock sync state "
			      f"(reads {datetime.now():%Y-%m-%d %H:%M}).")
		print("VoiceCommandHandler: initialized.")

	FOLLOWUP_INTENTS = ("stop",)	

	def parse(self, transcript: str, followup: bool = False) -> bool:
		"""
		Attempt to detect a command in the transcript.
		Returns True if a confident match was found and handled, False otherwise.
		"""
		text = transcript.lower().strip().rstrip(".,!?")

		song_name = self._match_play_by_name(text)
		if song_name is not None:
			if self._handle_play_by_name(song_name):
				return True

		ssid_name = self._match_connect_wifi(text)
		if ssid_name is not None:
			self._handle_connect_wifi(ssid_name)
			return True

		# 3. Fuzzy match against all intent phrases
		phrase_map = self._phrase_map
		if followup:
			phrase_map = [(p, n) for p, n in phrase_map if n in self.FOLLOWUP_INTENTS]
		phrase_strings = [p[0] for p in phrase_map]

		match = process.extractOne(text, phrase_strings, scorer=fuzz.ratio) if phrase_strings else None
		if match and match[1] >= self.CONFIDENCE_THRESHOLD:
			intent_name = next(name for phrase, name in phrase_map if phrase == match[0])
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
			"sing":             self._handle_sing,
			"stop":				self._handle_stop,
			"get_ip":           self._handle_get_ip,
			"get_wifi_network": self._handle_get_wifi_network,
			"get_time":         self._handle_get_time,
			"get_day":          self._handle_get_day,
			"get_month":        self._handle_get_month,
			"get_date":         self._handle_get_date,
			"get_year":         self._handle_get_year,
			"who_are_you":      self._handle_who_are_you,
			"greeting":         self._handle_greeting,
		}
		handler = handlers.get(intent)
		if handler:
			dispatcher.send(signal="updateStatus", id="Command", value=intent)
			handler()

	# --- intent handlers ---

	def _handle_sing(self) -> None:
		print("VoiceCommandHandler: sing")
		show_list = self._show_player.show_list
		if not show_list:
			print("VoiceCommandHandler: no shows available.")
			return
		show_name = random.choice(show_list)
		print(f"VoiceCommandHandler: randomly selected show '{show_name}'")
		dispatcher.send(signal='showStatus', status='play', show_name=show_name)

	def _handle_stop(self) -> None:
		print("VoiceCommandHandler: stop")
		dispatcher.send(signal='showStatus', status='stop')

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

		# Offline only: the ellipsis pauses that space the digits out are
		# inserted by the Piper path, and ElevenLabs would read straight past
		# them at its own cadence.
		dispatcher.send(signal="executeTTS", text=self._spell_ip(ip), bForceOffline=True)

	@staticmethod
	def _spell_ip(ip: str) -> str:
		"""Space an IP out one digit at a time, joined by ellipses.

		The ellipses are what the offline TTS splits on to insert real
		silence, so the digits land slowly enough to write down. "0" is
		written out because a bare digit on its own reads as "oh" as often
		as "zero".
		"""
		parts = ["My IP address is"]
		octets = ip.split(".")
		for index, octet in enumerate(octets):
			for digit in octet:
				parts.append("zero" if digit == "0" else digit)
			if index < len(octets) - 1:
				parts.append("dot")
		return "... ".join(parts)

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

	# Below this the clock is obviously unset — the Orin Nano dev kit has no
	# battery-backed RTC, so a cold boot with no network lands in 1970 or at
	# whatever the image was built with.
	_MIN_PLAUSIBLE_YEAR = 2024

	_MONTHS = [
		"January", "February", "March", "April", "May", "June",
		"July", "August", "September", "October", "November", "December",
	]

	@classmethod
	def _clock_is_trustworthy(cls) -> bool:
		"""Sanity-check the system clock before speaking a date aloud.

		The year test is primary because it catches the failure that actually
		happens. timedatectl is consulted only to log why — an un-synced clock
		that still reads plausibly is usually fine, so it is not grounds to
		refuse on its own.
		"""
		if datetime.now().year >= cls._MIN_PLAUSIBLE_YEAR:
			return True
		print("VoiceCommandHandler: system clock reads "
		      f"{datetime.now():%Y-%m-%d %H:%M} — refusing to state the time.")
		return False

	@staticmethod
	def _ntp_synchronized() -> Optional[bool]:
		"""True/False from timedatectl, or None if it cannot be determined."""
		try:
			result = subprocess.run(
				["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
				capture_output=True, text=True, timeout=2,
			)
		except Exception:
			return None
		value = result.stdout.strip()
		return value == "yes" if value in ("yes", "no") else None

	def _say_unknown_time(self) -> None:
		dispatcher.send(signal="executeTTS", bForceOffline=True, text=random.choice([
			"Oh gee... I've completely lost track of time.",
			"Hmm... my clock isn't set, so I honestly couldn't tell you.",
			"Well now... I'm afraid I don't know what time it is.",
		]))

	def _handle_get_time(self) -> None:
		if not self._clock_is_trustworthy():
			self._say_unknown_time()
			return
		now = datetime.now()
		hour = now.hour % 12 or 12
		if now.hour < 12:
			part = "in the morning"
		elif now.hour < 17:
			part = "in the afternoon"
		elif now.hour < 21:
			part = "in the evening"
		else:
			part = "at night"
		dispatcher.send(signal="executeTTS", bForceOffline=True, text=random.choice([
			f"It's {hour}:{now.minute:02d} {part}.",
			f"Well, it's about {hour}:{now.minute:02d} {part}.",
			f"Right now it's {hour}:{now.minute:02d} {part}.",
		]))

	def _handle_get_day(self) -> None:
		if not self._clock_is_trustworthy():
			self._say_unknown_time()
			return
		day = datetime.now().strftime("%A")
		dispatcher.send(signal="executeTTS", bForceOffline=True, text=random.choice([
			f"It's {day}.",
			f"Today is {day}.",
			f"Why, it's {day}!",
		]))

	def _handle_get_month(self) -> None:
		if not self._clock_is_trustworthy():
			self._say_unknown_time()
			return
		month = self._MONTHS[datetime.now().month - 1]
		dispatcher.send(signal="executeTTS", bForceOffline=True, text=random.choice([
			f"It's {month}.",
			f"We're in {month}.",
		]))

	def _handle_get_date(self) -> None:
		if not self._clock_is_trustworthy():
			self._say_unknown_time()
			return
		now = datetime.now()
		month = self._MONTHS[now.month - 1]
		dispatcher.send(signal="executeTTS", bForceOffline=True, text=random.choice([
			f"It's {now.strftime('%A')}, {month} {self._ordinal(now.day)}, {now.year}.",
			f"Today is {month} {self._ordinal(now.day)}, {now.year}.",
		]))

	def _handle_get_year(self) -> None:
		if not self._clock_is_trustworthy():
			self._say_unknown_time()
			return
		dispatcher.send(signal="executeTTS", bForceOffline=True, text=random.choice([
			f"It's {datetime.now().year}.",
			f"Why, it's {datetime.now().year}!",
		]))

	@staticmethod
	def _ordinal(day: int) -> str:
		if 11 <= day % 100 <= 13:
			return f"{day}th"
		return f"{day}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th') }"

	def _handle_who_are_you(self) -> None:
		dispatcher.send(signal="playVoiceFile", file="who_are_you.ogg")

	def _handle_greeting(self) -> None:
		print("VoiceCommandHandler: greeting")
		dispatcher.send(signal="playVoiceFile", file="who_are_you.ogg")