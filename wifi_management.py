import subprocess
import threading
import time
import configparser
import difflib
from typing import Optional, List, Dict
from pydispatch import dispatcher

WIFI_INTERFACE = "wlan0"


class WifiManagement:
	def __init__(self) -> None:
		self._monitor_thread: Optional[threading.Thread] = None
		self._stop_monitor = threading.Event()
		self._cached_networks: List[Dict] = []

		self._start_disconnect_monitor()

		print("WifiManagement: initialized.")

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

	def apply_config(self, path):
		config = configparser.ConfigParser()
		try:
			config.read(path)
		except configparser.Error as e:
			print(f"WifiManagement: failed to parse config at '{path}': {e}")
			return

		preferred_ssid = config.get("WiFi", "WifiName", fallback="").strip()
		preferred_password = config.get("WiFi", "Password", fallback="").strip() or None

		if not preferred_ssid:
			print("WifiManagement: apply_config — no WifiName set, doing nothing.")
			return

		if preferred_password:
			print(f"WifiManagement: apply_config — connecting to '{preferred_ssid}' with password...")
		else:
			print(f"WifiManagement: apply_config — connecting to '{preferred_ssid}' (no password)...")

		threading.Thread(
			target=self._startup_connect,
			args=(preferred_ssid, preferred_password),
			daemon=True
		).start()

	def scan(self) -> None:
		"""Trigger a WiFi scan in a background thread.
		Dispatches 'wifiScanComplete' with a list of dicts: [{ssid, signal_strength}]
		when finished."""
		threading.Thread(target=self._do_scan, daemon=True).start()

	def get_wifi_access_points(self) -> List[Dict]:
		"""Return the cached list of networks from the last completed scan.
		Each entry is a dict with 'ssid' and 'signal_strength' keys.
		Returns an empty list if no scan has completed yet."""
		return self._cached_networks

	def get_current_ssid(self) -> Optional[str]:
		"""Return the SSID of the currently connected network, or None."""
		try:
			# Query active connections directly — much faster than 'dev wifi' which hits the radio
			result = subprocess.run(
				["nmcli", "-t", "-f", "NAME,TYPE,STATE", "con", "show", "--active"],
				capture_output=True, text=True, timeout=5
			)
			for line in result.stdout.splitlines():
				parts = line.split(":", 2)
				if len(parts) == 3 and "wireless" in parts[1].lower() and "activated" in parts[2].lower():
					return parts[0].strip() or None
		except Exception as e:
			print(f"WifiManagement: error getting current SSID: {e}")
		return None

	def get_signal_strength(self) -> int:
		"""Return the current WiFi signal strength as a percentage (0-100), or 0.
		Uses 'iw' to read the kernel's cached RSSI — no radio scan involved."""
		try:
			result = subprocess.run(
				["iw", "dev", WIFI_INTERFACE, "link"],
				capture_output=True, text=True, timeout=5
			)
			for line in result.stdout.splitlines():
				line = line.strip()
				if line.startswith("signal:"):
					# Value is in dBm, e.g. "signal: -55 dBm"
					dbm = int(line.split()[1])
					# Convert dBm to percentage: -30 dBm = 100%, -90 dBm = 0%
					percent = max(0, min(100, 2 * (dbm + 100)))
					return percent
		except Exception as e:
			print(f"WifiManagement: error getting signal strength: {e}")
		return 0

	def connect(self, ssid_query: str, password: Optional[str] = None) -> None:
		"""Connect to a WiFi network by fuzzy-matched name.
		- If previously connected: nmcli brings the saved profile up (no password needed).
		- If new network and password provided: connects and saves credentials via NM.
		- If new network and no password: dispatches 'wifiPasswordRequired'.
		Dispatches 'wifiConnected' on success, 'wifiWrongPassword' on auth failure."""
		threading.Thread(
			target=self._do_connect,
			args=(ssid_query, password),
			daemon=True
		).start()

	# -------------------------------------------------------------------------
	# Internal: scanning
	# -------------------------------------------------------------------------

	def _do_scan(self) -> None:
		print("WifiManagement: _do_scan started")
		try:
			print("WifiManagement: scanning...")
			result = subprocess.run(
				["nmcli", "--terse", "-f", "SSID,SIGNAL,IN-USE", "dev", "wifi", "list", "--rescan", "yes"],
				capture_output=True, text=True, timeout=30
			)
			networks: List[Dict] = []
			seen = set()
			for line in result.stdout.splitlines():
				parts = line.split(":", 2)
				if len(parts) < 3:
					continue
				ssid = parts[0].strip()
				signal = parts[1].strip()
				if not ssid or ssid in seen:
					continue
				seen.add(ssid)
				try:
					networks.append({"ssid": ssid, "signal_strength": int(signal)})
				except ValueError:
					pass

			networks.sort(key=lambda x: x["signal_strength"], reverse=True)
			self._cached_networks = networks
			print(f"WifiManagement: scan complete, {len(networks)} networks found.")
			dispatcher.send(signal="wifiScanComplete", networks=networks)
		except Exception as e:
			print(f"WifiManagement: scan error: {e}")
			dispatcher.send(signal="wifiScanComplete", networks=[])

	# -------------------------------------------------------------------------
	# Internal: connecting
	# -------------------------------------------------------------------------

	def _do_connect(self, ssid_query: str, password: Optional[str]) -> None:
		# Resolve fuzzy match against currently visible networks
		target_ssid = self._fuzzy_match_ssid(ssid_query)
		if not target_ssid:
			print(f"WifiManagement: no match found for '{ssid_query}'")
			return

		print(f"WifiManagement: connecting to '{target_ssid}'...")

		# Check if NetworkManager already has a saved profile for this SSID
		known_profiles = self._get_known_ssids()
		is_known = target_ssid in known_profiles

		if is_known:
			# NM has stored credentials — just bring it up
			success, error = self._nmcli_up_by_ssid(target_ssid)
		elif password:
			# New network with a password — connect and let NM save it
			success, error = self._nmcli_connect_new(target_ssid, password)
		else:
			# TODO: password required to connect to this new network
			print(f"WifiManagement: password required for new network '{target_ssid}'")
			dispatcher.send(signal="wifiPasswordRequired", ssid=target_ssid)
			return

		if success:
			print(f"WifiManagement: connected to '{target_ssid}'.")
			dispatcher.send(signal="wifiConnected", ssid=target_ssid)
		else:
			if error and ("secrets" in error.lower() or "password" in error.lower() or "802-11" in error.lower()):
				print(f"WifiManagement: wrong password for '{target_ssid}'.")
				dispatcher.send(signal="wifiWrongPassword", ssid=target_ssid)
			else:
				print(f"WifiManagement: connection failed for '{target_ssid}': {error}")

	def _nmcli_up_by_ssid(self, ssid: str):
		"""Bring up a known NM connection profile by SSID."""
		try:
			result = subprocess.run(
				["nmcli", "dev", "wifi", "connect", ssid],
				capture_output=True, text=True, timeout=30
			)
			success = result.returncode == 0
			error = result.stderr.strip() if not success else None
			return success, error
		except Exception as e:
			return False, str(e)

	def _nmcli_connect_new(self, ssid: str, password: str):
		"""Connect to a new network with a password; NM will save the profile."""
		try:
			result = subprocess.run(
				["nmcli", "dev", "wifi", "connect", ssid, "password", password],
				capture_output=True, text=True, timeout=30
			)
			success = result.returncode == 0
			error = result.stderr.strip() if not success else None
			return success, error
		except Exception as e:
			return False, str(e)

	# -------------------------------------------------------------------------
	# Internal: disconnect monitor
	# -------------------------------------------------------------------------

	def _start_disconnect_monitor(self) -> None:
		self._stop_monitor.clear()
		self._monitor_thread = threading.Thread(
			target=self._monitor_loop, daemon=True
		)
		self._monitor_thread.start()

	def _monitor_loop(self) -> None:
		"""Watch nmcli monitor output for disconnection events on the wifi interface."""
		try:
			proc = subprocess.Popen(
				["nmcli", "monitor"],
				stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
				text=True
			)
			print("WifiManagement: disconnect monitor started.")
			while not self._stop_monitor.is_set():
				line = proc.stdout.readline()
				if not line:
					break
				line_lower = line.lower()
				# Only care about our wifi interface going disconnected
				if WIFI_INTERFACE in line_lower and "disconnected" in line_lower:
					print(f"WifiManagement: disconnected ({line.strip()})")
					dispatcher.send(signal="wifiDisconnected")
			proc.terminate()
		except Exception as e:
			print(f"WifiManagement: monitor error: {e}")

	# -------------------------------------------------------------------------
	# Internal: helpers
	# -------------------------------------------------------------------------

	def _get_known_ssids(self) -> List[str]:
		"""Return a list of SSIDs that NetworkManager has saved profiles for."""
		try:
			result = subprocess.run(
				["nmcli", "-t", "-f", "NAME,TYPE", "con", "show"],
				capture_output=True, text=True, timeout=5
			)
			ssids = []
			for line in result.stdout.splitlines():
				parts = line.split(":", 1)
				if len(parts) == 2 and "wireless" in parts[1].lower():
					ssids.append(parts[0].strip())
			return ssids
		except Exception as e:
			print(f"WifiManagement: error fetching known SSIDs: {e}")
			return []

	def _get_visible_ssids(self) -> List[str]:
		"""Return SSIDs currently visible in the air (no rescan)."""
		try:
			result = subprocess.run(
				["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list"],
				capture_output=True, text=True, timeout=10
			)
			return [
				line.strip() for line in result.stdout.splitlines()
				if line.strip()
			]
		except Exception as e:
			print(f"WifiManagement: error listing visible SSIDs: {e}")
			return []

	def _fuzzy_match_ssid(self, query: str) -> Optional[str]:
		"""Find the best-matching SSID from visible networks using difflib."""
		candidates = self._get_visible_ssids()
		if not candidates:
			return None

		# Exact match first (case-insensitive)
		for ssid in candidates:
			if ssid.lower() == query.lower():
				return ssid

		matches = difflib.get_close_matches(query, candidates, n=1, cutoff=0.4)
		if matches:
			return matches[0]

		# Last resort: substring match
		for ssid in candidates:
			if query.lower() in ssid.lower() or ssid.lower() in query.lower():
				return ssid

		return None

	def _startup_connect(self, ssid: str, password: Optional[str]) -> None:
		"""Try preferred SSID; fall back to known NM profiles on failure."""
		current = self.get_current_ssid()
		target = self._fuzzy_match_ssid(ssid)
		if target and current and current.lower() == target.lower():
			print(f"WifiManagement: already connected to '{current}', skipping.")
			dispatcher.send(signal="wifiConnected", ssid=current)
			return
		if target:
			known = self._get_known_ssids()
			if target in known:
				success, _ = self._nmcli_up_by_ssid(target)
			elif password:
				success, _ = self._nmcli_connect_new(target, password)
			else:
				success = False

			if success:
				print(f"WifiManagement: connected to preferred network '{target}'.")
				dispatcher.send(signal="wifiConnected", ssid=target)
				return

		print("WifiManagement: preferred network failed, trying known networks...")
		self._fallback_connect()

	def _fallback_connect(self) -> None:
		"""Iterate NM's saved wireless profiles and try to connect to each."""
		current = self.get_current_ssid()
		if current:
			print(f"WifiManagement: already connected to '{current}', skipping fallback.")
			dispatcher.send(signal="wifiConnected", ssid=current)
			return

		known = self._get_known_ssids()
		for ssid in known:
			print(f"WifiManagement: trying saved network '{ssid}'...")
			success, _ = self._nmcli_up_by_ssid(ssid)
			if success:
				print(f"WifiManagement: connected via fallback to '{ssid}'.")
				dispatcher.send(signal="wifiConnected", ssid=ssid)
				return
			time.sleep(1)
		print("WifiManagement: all fallback networks failed.")


# Example usage
if __name__ == "__main__":
	wifi = WifiManagement()
	wifi.scan()
	time.sleep(10)