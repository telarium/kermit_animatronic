#!/usr/bin/env python3
"""
peer_network.py — finding other characters and keeping clocks lined up.

Discovery is peer-to-peer mDNS (python-zeroconf), service type
_animatronic._tcp. Every character registers itself and browses for the rest;
there is no broker. The TXT record carries the display name, serial, IP and
ports, plus a short character description. Larger details come from the
/peer/info HTTP endpoint. get_all_characters() lists everyone, this
character included, for any module that wants it.

One UDP socket (SYNC_PORT) carries everything timing-sensitive:
	ping / pong  — clock sync. Every character pings every peer once a second.
	               The lowest-round-trip samples give the offset between the
	               two monotonic clocks.
	hb / end     — show heartbeats and show ends from a host. Not handled
	               here; they are passed to the on_message callback.

PeerConversation, at the end of this file, shares LLM responses between
characters and coordinates who speaks which line of them.

Clock offsets are between time.monotonic() on each machine. Nothing here
depends on wall-clock time, so NTP state doesn't matter.
"""

import collections
import json
import os
import re
import socket
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple

import requests
from pydispatch import dispatcher

from utils import get_local_ip
from logger import get_logger

log = get_logger(__name__)

try:
	from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf
	_ZEROCONF_AVAILABLE = True
except ImportError:
	_ZEROCONF_AVAILABLE = False


SERVICE_TYPE = "_animatronic._tcp.local."
SYNC_PORT = 47811
WEB_PORT = 80

PING_INTERVAL_S = 1.0
# Samples kept per peer. The offset comes from the lowest-RTT sample among
# them, which throws away the ones delayed by WiFi retries.
SAMPLE_WINDOW = 10
# A peer that hasn't answered a ping in this long is hidden from the list.
# mDNS alone would keep a crashed character listed until its TTL expired.
PEER_STALE_S = 10.0
# Newly discovered peers are listed for this long before they must answer.
DISCOVERY_GRACE_S = 10.0
# Ping-time updates to the web UI are throttled to this; membership changes
# are pushed immediately.
LIST_REFRESH_S = 3.0

# How often to re-check our own address. Catches DHCP changes and a network
# that only came up after startup, whatever caused them.
IP_CHECK_S = 5.0

# Each TXT entry is capped at 255 bytes including "key=". Descriptions longer
# than this are cut at a character boundary for the announcement.
MAX_TXT_VALUE_BYTES = 240

RESOLVE_TIMEOUT_MS = 3000
POST_TIMEOUT_S = 5.0
MAX_DATAGRAM = 8192


def _fit_txt(value: str) -> str:
	data = value.encode("utf-8")
	if len(data) <= MAX_TXT_VALUE_BYTES:
		return value
	return data[:MAX_TXT_VALUE_BYTES].decode("utf-8", errors="ignore").rstrip() + "…"


class Peer:
	def __init__(self, serial: str, name: str, ip: str, web_port: int, sync_port: int,
	             description: str = "") -> None:
		self.serial = serial
		self.name = name
		self.description = description
		self.ip = ip
		self.web_port = web_port
		self.sync_port = sync_port
		self.discovered_at = time.monotonic()
		self.last_pong: float = 0.0
		self.rtt_ms: Optional[float] = None
		# (rtt_s, offset_s) pairs, newest last.
		self.samples: collections.deque = collections.deque(maxlen=SAMPLE_WINDOW)

	def is_alive(self, now: float) -> bool:
		if self.last_pong:
			return now - self.last_pong < PEER_STALE_S
		return now - self.discovered_at < DISCOVERY_GRACE_S

	def offset(self) -> Optional[float]:
		"""Peer's monotonic clock minus ours, in seconds, from the best sample."""
		if not self.samples:
			return None
		return min(self.samples, key=lambda s: s[0])[1]

	def to_ui(self) -> dict:
		return {
			"name": self.name,
			"description": self.description,
			"ip": self.ip,
			"port": self.web_port,
			"ping_ms": round(self.rtt_ms) if self.rtt_ms is not None else None,
		}


# The running PeerNetwork, so modules that don't hold a reference to it (like
# llm_service.py) can still ask who's around. Set by PeerNetwork.start().
_active: Optional["PeerNetwork"] = None


# TESTING ONLY. Start with ANIMATRONIC_FAKE_PEERS=1 in the environment and
# get_all_characters() adds these, as if they were on the network. They exist
# nowhere else: not in the peer list or web UI, and nothing is sent to them.
FAKE_PEERS_ENV = "ANIMATRONIC_FAKE_PEERS"
_FAKE_CHARACTERS = [
	{
		"name": "Fozzie Bear",
		"description": "Fozzie Bear from The Muppet Show, a stand-up comedian who tells "
		               "terrible jokes, says 'Wocka wocka!', and is Kermit's best friend.",
		"serial": "fake00000001",
		"ip": "192.0.2.1",
		"is_self": False,
	},
	{
		"name": "Miss Piggy",
		"description": "Miss Piggy from The Muppet Show, a glamorous diva who adores Kermit, "
		               "speaks of herself as 'moi', and has a famous karate chop.",
		"serial": "fake00000002",
		"ip": "192.0.2.2",
		"is_self": False,
	},
	{
		"name": "Gonzo",
		"description": "Gonzo the Great from The Muppet Show, a daredevil performance artist "
		               "who loves chickens and bizarre stunts.",
		"serial": "fake00000003",
		"ip": "192.0.2.3",
		"is_self": False,
	},
]


def _fake_peers_enabled() -> bool:
	return os.environ.get(FAKE_PEERS_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def get_all_characters(include_self: bool = True) -> List[dict]:
	"""Every character on the network: [{name, description, serial, ip,
	is_self}], this one first. Empty before peer networking has started.
	Safe to call from any thread."""
	network = _active
	if network is None:
		return []
	characters = network.characters(include_self)
	if _fake_peers_enabled():
		characters += [dict(c) for c in _FAKE_CHARACTERS]
	return characters


class PeerNetwork:
	def __init__(
		self,
		identity,
		on_peers_changed: Optional[Callable[[List[dict]], None]] = None,
		on_peer_added: Optional[Callable[[Peer], None]] = None,
		on_message: Optional[Callable[[dict, Tuple[str, int]], None]] = None,
	) -> None:
		self.identity = identity
		self.on_peers_changed = on_peers_changed
		self.on_peer_added = on_peer_added
		self.on_message = on_message

		self.ip: Optional[str] = None
		self.interface: Optional[str] = None

		self._peers: Dict[str, Peer] = {}
		self._names: Dict[str, str] = {}		# mDNS service name -> serial
		self._lock = threading.RLock()
		self._seq = 0

		self._zc = None
		self._browser = None
		self._info = None
		self._zc_lock = threading.Lock()

		self._sock: Optional[socket.socket] = None
		self._stop_event = threading.Event()
		self._last_alive: set = set()
		self._last_list_push = 0.0

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

	def start(self, ip: Optional[str], interface: Optional[str] = None) -> None:
		global _active
		_active = self
		if _fake_peers_enabled():
			log.warning(f"PeerNetwork: {FAKE_PEERS_ENV} is set — get_all_characters() includes "
			      f"{len(_FAKE_CHARACTERS)} fake characters for testing.")
		self.ip = ip
		self.interface = interface
		self._open_socket()
		threading.Thread(target=self._receive_loop, daemon=True).start()
		threading.Thread(target=self._ping_loop, daemon=True).start()
		# Registration can block for a couple of seconds while zeroconf
		# probes, so it never runs on the startup path.
		threading.Thread(target=self._start_zeroconf, daemon=True).start()

	def update_ip(self, ip: Optional[str], interface: Optional[str] = None) -> None:
		"""Call when the network changes. Restarts mDNS so the announcement
		carries the new address and multicast is joined on the new interface."""
		if ip == self.ip:
			return
		log.info(f"PeerNetwork: address changed {self.ip} -> {ip} ({interface})")
		self.ip = ip
		self.interface = interface
		threading.Thread(target=self._start_zeroconf, daemon=True).start()

	def stop(self) -> None:
		self._stop_event.set()
		self._stop_zeroconf()
		if self._sock:
			try:
				self._sock.close()
			except OSError:
				pass

	def peers(self) -> List[Peer]:
		"""Peers currently considered alive, sorted by name."""
		now = time.monotonic()
		with self._lock:
			alive = [p for p in self._peers.values() if p.is_alive(now)]
		return sorted(alive, key=lambda p: (p.name.lower(), p.serial))

	def get_peer(self, serial: str) -> Optional[Peer]:
		with self._lock:
			return self._peers.get(serial)

	def offset(self, serial: str) -> Optional[float]:
		peer = self.get_peer(serial)
		return peer.offset() if peer else None

	def characters(self, include_self: bool = True) -> List[dict]:
		"""Every character currently on the network, this one first when
		include_self is set. Each entry: name, description, serial, ip, and
		is_self."""
		result = []
		if include_self:
			result.append({
				"name": self.identity.name,
				"description": self.identity.description,
				"serial": self.identity.serial,
				"ip": self.ip,
				"is_self": True,
			})
		for peer in self.peers():
			result.append({
				"name": peer.name,
				"description": peer.description,
				"serial": peer.serial,
				"ip": peer.ip,
				"is_self": False,
			})
		return result

	def ui_list(self) -> List[dict]:
		return [p.to_ui() for p in self.peers()]

	def send(self, address: Tuple[str, int], message: dict) -> None:
		if not self._sock:
			return
		try:
			data = json.dumps(message, separators=(",", ":")).encode("utf-8")
			self._sock.sendto(data, address)
		except OSError as e:
			log.warning(f"PeerNetwork: send to {address[0]} failed: {e}")

	def send_to_peer(self, peer: Peer, message: dict) -> None:
		self.send((peer.ip, peer.sync_port), message)

	def post_to_peers(self, path: str, payload: dict) -> int:
		"""POST JSON to `path` on every other character's web server, each on
		its own thread so a slow or missing peer never holds up the caller.
		For messages that must arrive intact and aren't timing-critical;
		anything timing-critical goes over the UDP socket instead. Returns
		how many peers it was sent to."""
		peers = self.peers()
		for peer in peers:
			threading.Thread(target=self._post, args=(peer, path, payload), daemon=True).start()
		return len(peers)

	@staticmethod
	def _post(peer: Peer, path: str, payload: dict) -> None:
		url = f"http://{peer.ip}:{peer.web_port}{path}"
		try:
			response = requests.post(url, json=payload, timeout=POST_TIMEOUT_S)
			if response.status_code != 200:
				log.warning(f"PeerNetwork: {path} to '{peer.name}' returned {response.status_code}.")
		except Exception as e:
			log.warning(f"PeerNetwork: {path} to '{peer.name}' at {peer.ip} failed: {e}")

	# -------------------------------------------------------------------------
	# mDNS
	# -------------------------------------------------------------------------

	def _start_zeroconf(self) -> None:
		if not _ZEROCONF_AVAILABLE:
			log.error("PeerNetwork: python zeroconf is not installed — discovery disabled. Run setup.py.")
			return
		with self._zc_lock:
			self._stop_zeroconf_locked()
			if not self.ip:
				log.warning("PeerNetwork: no network address yet — mDNS waits for a connection.")
				return
			try:
				# Bound to the one address we advertise, so nothing is
				# announced or browsed on VPN or USB-bridge interfaces.
				self._zc = Zeroconf(interfaces=[self.ip], ip_version=IPVersion.V4Only)
				self._info = self._build_service_info()
				self._zc.register_service(self._info)
				self._browser = ServiceBrowser(self._zc, SERVICE_TYPE, handlers=[self._on_service_change])
				log.info(f"PeerNetwork: announced '{self.identity.instance_name}' at {self.ip}")
			except Exception as e:
				log.exception(f"PeerNetwork: mDNS start failed: {e}")
				self._stop_zeroconf_locked()

	def _stop_zeroconf(self) -> None:
		with self._zc_lock:
			self._stop_zeroconf_locked()

	def _stop_zeroconf_locked(self) -> None:
		if self._zc is None:
			return
		try:
			if self._browser is not None:
				self._browser.cancel()
			if self._info is not None:
				# Sends the goodbye, so peers drop us immediately.
				self._zc.unregister_service(self._info)
			self._zc.close()
		except Exception as e:
			log.warning(f"PeerNetwork: mDNS stop: {e}")
		self._zc = None
		self._browser = None
		self._info = None

	def _build_service_info(self):
		ident = self.identity
		properties = {
			"name": ident.name,
			"desc": _fit_txt(ident.description),
			"serial": ident.serial,
			"ip": self.ip,
			"web": str(WEB_PORT),
			"sync": str(SYNC_PORT),
		}
		return ServiceInfo(
			SERVICE_TYPE,
			f"{ident.instance_name}.{SERVICE_TYPE}",
			addresses=[socket.inet_aton(self.ip)],
			port=WEB_PORT,
			properties=properties,
			# A hostname of our own, so this never contends with the
			# <hostname>.local record Avahi already publishes.
			server=f"animatronic-{ident.short_serial}.local.",
		)

	def _on_service_change(self, zeroconf, service_type, name, state_change) -> None:
		# Called on zeroconf's own thread, which must not block — resolving
		# does, so it happens on a worker.
		if state_change is ServiceStateChange.Removed:
			self._remove_by_name(name)
		else:
			threading.Thread(target=self._resolve, args=(zeroconf, service_type, name), daemon=True).start()

	def _resolve(self, zeroconf, service_type: str, name: str) -> None:
		try:
			info = zeroconf.get_service_info(service_type, name, timeout=RESOLVE_TIMEOUT_MS)
		except Exception as e:
			log.warning(f"PeerNetwork: could not resolve '{name}': {e}")
			return
		if info is None:
			return

		props = {}
		for k, v in (info.properties or {}).items():
			key = k.decode("utf-8", errors="ignore") if isinstance(k, bytes) else str(k)
			val = v.decode("utf-8", errors="ignore") if isinstance(v, bytes) else (v or "")
			props[key] = val

		serial = props.get("serial", "")
		if not serial or serial == self.identity.serial:
			return

		addresses = [a for a in info.parsed_addresses() if ":" not in a]
		resolved_ip = addresses[0] if addresses else props.get("ip", "")
		if not resolved_ip:
			return
		reported_ip = props.get("ip", "")
		if reported_ip and reported_ip != resolved_ip:
			log.warning(f"PeerNetwork: '{props.get('name')}' reports {reported_ip} but resolves "
			      f"to {resolved_ip} — likely an interface mix-up on that character.")

		try:
			web_port = int(props.get("web") or info.port or WEB_PORT)
			sync_port = int(props.get("sync") or SYNC_PORT)
		except ValueError:
			web_port, sync_port = WEB_PORT, SYNC_PORT

		added = None
		with self._lock:
			self._names[name] = serial
			peer = self._peers.get(serial)
			if peer is None:
				peer = Peer(serial, props.get("name") or serial, resolved_ip, web_port, sync_port,
					props.get("desc", ""))
				self._peers[serial] = peer
				added = peer
			else:
				peer.name = props.get("name") or peer.name
				peer.description = props.get("desc", "")
				peer.ip = resolved_ip
				peer.web_port = web_port
				peer.sync_port = sync_port

		if added:
			log.info(f"PeerNetwork: found '{added.name}' ({added.serial[-6:]}) at {added.ip}")
			if self.on_peer_added:
				try:
					self.on_peer_added(added)
				except Exception as e:
					log.exception(f"PeerNetwork: on_peer_added failed: {e}")
		self._push_list(force=True)

	def _remove_by_name(self, name: str) -> None:
		with self._lock:
			serial = self._names.pop(name, None)
			peer = self._peers.pop(serial, None) if serial else None
		if peer:
			log.info(f"PeerNetwork: '{peer.name}' left the network.")
			self._push_list(force=True)

	# -------------------------------------------------------------------------
	# UDP: clock sync + message routing
	# -------------------------------------------------------------------------

	def _open_socket(self) -> None:
		sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		sock.bind(("0.0.0.0", SYNC_PORT))
		sock.settimeout(0.5)
		self._sock = sock
		log.info(f"PeerNetwork: sync socket on UDP {SYNC_PORT}")

	def _receive_loop(self) -> None:
		while not self._stop_event.is_set():
			try:
				data, address = self._sock.recvfrom(MAX_DATAGRAM)
			except socket.timeout:
				continue
			except OSError:
				if self._stop_event.is_set():
					return
				time.sleep(0.1)
				continue

			received = time.monotonic()
			try:
				message = json.loads(data.decode("utf-8"))
			except ValueError:
				continue
			if not isinstance(message, dict):
				continue

			kind = message.get("t")
			if kind == "ping":
				self.send(address, {
					"t": "pong",
					"from": self.identity.serial,
					"seq": message.get("seq"),
					"t0": message.get("t0"),
					"r": time.monotonic(),
				})
			elif kind == "pong":
				self._on_pong(message, received)
			elif self.on_message:
				message["_rx"] = received
				try:
					self.on_message(message, address)
				except Exception as e:
					log.exception(f"PeerNetwork: message handler failed: {e}")

	def _on_pong(self, message: dict, received: float) -> None:
		try:
			t0 = float(message["t0"])
			remote = float(message["r"])
		except (KeyError, TypeError, ValueError):
			return
		rtt = received - t0
		if rtt < 0 or rtt > 2.0:
			return
		offset = remote - (t0 + received) / 2.0
		with self._lock:
			peer = self._peers.get(message.get("from", ""))
			if peer is None:
				return
			peer.samples.append((rtt, offset))
			peer.rtt_ms = rtt * 1000.0
			peer.last_pong = received

	def _ping_loop(self) -> None:
		next_ip_check = time.monotonic() + IP_CHECK_S
		while not self._stop_event.is_set():
			now = time.monotonic()
			if now >= next_ip_check:
				next_ip_check = now + IP_CHECK_S
				ip, interface = get_local_ip()
				self.update_ip(ip, interface)
			with self._lock:
				targets = list(self._peers.values())
			for peer in targets:
				self._seq += 1
				self.send_to_peer(peer, {
					"t": "ping",
					"from": self.identity.serial,
					"seq": self._seq,
					"t0": time.monotonic(),
				})

			alive = {p.serial for p in self.peers()}
			if alive != self._last_alive:
				self._last_alive = alive
				self._push_list(force=True)
			elif now - self._last_list_push >= LIST_REFRESH_S:
				self._push_list(force=False)

			self._stop_event.wait(PING_INTERVAL_S)

	def _push_list(self, force: bool) -> None:
		if not self.on_peers_changed:
			return
		now = time.monotonic()
		if not force and now - self._last_list_push < LIST_REFRESH_S:
			return
		self._last_list_push = now
		try:
			self.on_peers_changed(self.ui_list())
		except Exception as e:
			log.exception(f"PeerNetwork: on_peers_changed failed: {e}")


# =============================================================================
# Speaker lines — splitting a multi-character LLM response
# =============================================================================
#
# When other characters are present, the LLM may write a short exchange:
#
#	Hi-ho! Should I tell a joke, Fozzie?
#	{Fozzie Bear} Yes, I'd love to hear it!
#	{Kermit the Frog} Never mind, I forgot.
#
# Each {Name} starts a new line spoken by that character. Text before the first
# tag belongs to the character that got the response. Every character splits
# the same text with this same function, so line numbers agree everywhere —
# they are what the "spoken" broadcasts refer to.

_SPEAKER_TAG_RE = re.compile(r"\{([^{}\n]+)\}")


def split_speaker_lines(text: str, default_speaker: str) -> List[Tuple[str, str]]:
	"""Split a response into [(speaker_name, line_text), ...] in order.
	Leading untagged text is default_speaker's. Empty lines are dropped, so
	a response that opens with a tag doesn't get an empty line 0."""
	parts = _SPEAKER_TAG_RE.split(text or "")
	lines: List[Tuple[str, str]] = []
	lead = parts[0].strip()
	if lead:
		lines.append((default_speaker, lead))
	for i in range(1, len(parts) - 1, 2):
		name = parts[i].strip()
		line = parts[i + 1].strip()
		if name and line:
			lines.append((name, line))
	return lines


def is_same_speaker(a: str, b: str) -> bool:
	"""Speaker names from the LLM should match character_name exactly, but
	case and stray spacing aren't worth failing over."""
	return " ".join((a or "").split()).casefold() == " ".join((b or "").split()).casefold()


# =============================================================================
# PeerConversation — sharing LLM responses between characters
# =============================================================================
#
# Groundwork for multi-character conversations. Two messages go to every other
# character (never back to this one), POSTed to /peer/speech:
#
#	response — the full LLM text, sent the moment it arrives and BEFORE this
#	           character starts speaking, so the others have the most time to
#	           prepare (synthesize) any lines of theirs.
#	spoken   — this character finished speaking a line, cleanly. Sent only if
#	           playback completed; speech that was cut off never cues the next
#	           speaker.
#
# Both carry the same utterance ID. "line" numbers the lines of a response from
# 0, in the order they appear, as split by split_speaker_lines(). Each "{Name}"
# in the text starts a new line spoken by that character; text before the first
# brace is line 0, spoken by the character that got the response. So far only
# line 0 is ever spoken, so "line" is always 0 — see the TODO(peer
# conversation) notes below.
#
# Talks to the rest of the system only through signals: it listens for
# llmResponse and voicePlaybackEvent, and speaks by dispatching executeTTS with
# an utterance_id.


class PeerConversation:
	SPEECH_PATH = "/peer/speech"

	def __init__(self, identity, peer_network: PeerNetwork,
	             can_speak: Optional[Callable[[], bool]] = None) -> None:
		self.identity = identity
		self.peer_network = peer_network
		# False while a show is running; responses are then ignored.
		self.can_speak = can_speak or (lambda: True)

		dispatcher.connect(self.on_llm_response, signal="llmResponse", sender=dispatcher.Any)
		dispatcher.connect(self.on_voice_playback_event, signal="voicePlaybackEvent", sender=dispatcher.Any)

	# -------------------------------------------------------------------------
	# This character's responses
	# -------------------------------------------------------------------------

	def on_llm_response(self, text: str) -> None:
		if not self.can_speak():
			return
		utterance_id = uuid.uuid4().hex[:12]

		# The full text goes out first, tags and all, so the other characters
		# have as long as possible to prepare their lines.
		sent = self.peer_network.post_to_peers(self.SPEECH_PATH, {
			"kind": "response",
			"utterance": utterance_id,
			"speaker": self._speaker(),
			"text": text,
		})
		if sent:
			log.info(f"Speech: sent response {utterance_id} to {sent} character(s).")

		lines = split_speaker_lines(text, self.identity.name)
		if not lines:
			return
		if len(lines) > 1:
			log.info(f"Speech: {utterance_id} has {len(lines)} lines: "
			      f"{', '.join(name for name, _ in lines)}")
			for index, (name, line) in enumerate(lines):
				who = "me" if is_same_speaker(name, self.identity.name) else name
				log.info(f"Speech:   line {index} ({who}): {line}")

		# This character speaks line 0 only. Its completion broadcasts
		# "spoken" line 0, which is the next speaker's cue.
		first_speaker, first_text = lines[0]
		if not is_same_speaker(first_speaker, self.identity.name):
			# TODO(peer conversation): the response opened with another
			# character's tag, so line 0 isn't ours. Nothing is spoken here and
			# nothing cues that character yet. Once receivers act on
			# "response", they should start line 0 themselves when it's theirs.
			log.warning(f"Speech: {utterance_id} opens with {first_speaker}'s line — "
			      f"not speaking it here.")
			return
		dispatcher.send(signal="executeTTS", text=first_text, utterance_id=utterance_id)

		# TODO(peer conversation): later lines of our own (a "{Kermit the
		# Frog}" tag after another character's line) should be synthesized
		# right now with TextToSpeech.synthesize() and cached by (utterance,
		# line), then played when the "spoken" message for the line before
		# arrives — the same path receivers use in _on_peer_speech.

		# TODO(peer conversation): when lines follow ours, this character
		# should stay quiet until the last line is spoken — no wakeword, no
		# listening — so the user doesn't talk over the other characters.
		# Right now start.py's on_voice_playback_event re-enables the wakeword
		# as soon as line 0 finishes.

	def on_voice_playback_event(self, bPlaying: bool, bCompleted: bool = False, tag: str = None) -> None:
		# The tag is the utterance ID, set only for speech that came from an
		# LLM response.
		if not bPlaying and bCompleted and tag:
			self.broadcast_spoken(tag)

	def broadcast_spoken(self, utterance_id: str, line: int = 0) -> None:
		sent = self.peer_network.post_to_peers(self.SPEECH_PATH, {
			"kind": "spoken",
			"utterance": utterance_id,
			"line": line,
			"speaker": self._speaker(),
		})
		if sent:
			log.info(f"Speech: finished line {line} of {utterance_id}, told {sent} character(s).")

	# -------------------------------------------------------------------------
	# Other characters' messages
	# -------------------------------------------------------------------------

	def receive(self, payload: dict, source_ip: str) -> dict:
		"""The /peer/speech handler. Runs on an HTTP thread."""
		kind = payload.get("kind")
		speaker = payload.get("speaker")
		if kind not in ("response", "spoken") or not isinstance(speaker, dict) \
				or not payload.get("utterance"):
			log.warning(f"Speech: malformed message from {source_ip}.")
			return {"ok": False, "reason": "malformed"}
		if speaker.get("serial") == self.identity.serial:
			# Our own message looped back somehow — never act on it.
			return {"ok": True, "reason": "own message"}
		self._on_peer_speech(payload)
		return {"ok": True}

	def _on_peer_speech(self, message: dict) -> None:
		"""Another character's speech. Only logged for now — this is where
		multi-character conversation logic goes."""
		speaker = message.get("speaker", {}).get("name", "?")
		if message.get("kind") == "response":
			lines = split_speaker_lines(message.get("text", ""), speaker)
			mine = [i for i, (name, _) in enumerate(lines) if is_same_speaker(name, self.identity.name)]
			log.info(f"Speech: {speaker} got response {message.get('utterance')} "
			      f"({len(lines)} line(s); mine: {mine or 'none'}): {message.get('text', '')!r}")
			# TODO(peer conversation): for each line in `mine`, synthesize it
			# now with TextToSpeech.synthesize() on a worker thread and cache
			# the file by (utterance, line), so it's ready the moment its cue
			# comes. If line 0 is ours (the response opened with our tag), play
			# it straight away.
		else:
			log.info(f"Speech: {speaker} finished line {message.get('line')} "
			      f"of {message.get('utterance')}.")
			# TODO(peer conversation): if line + 1 of that utterance is ours,
			# play the cached file (waiting for synthesis if it isn't done),
			# tagged so its completion broadcasts "spoken" for line + 1.

		# TODO(peer conversation), still to decide and build:
		#	- Lines naming a character that isn't present (or a misspelled
		#	  name) would stall the chain, since no one speaks them. Every
		#	  character can see that line N has no owner on the network and
		#	  treat it as already spoken.
		#	- A cue that never arrives (a character went offline mid-
		#	  conversation): time out, and delete the cached audio files.
		#	- A show starting mid-conversation: drop pending lines and cached
		#	  audio, and ignore conversation messages while performing in a
		#	  group show.
		#	- Should the other characters add the exchange to their own LLM
		#	  history, so they remember what they said?
		#	- Status in the web UI while a conversation is in progress.

	def _speaker(self) -> dict:
		return {"serial": self.identity.serial, "name": self.identity.name}
