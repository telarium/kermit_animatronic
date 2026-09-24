#!/usr/bin/env python3
"""
show_upload.py — shows uploaded from the web UI.

An uploaded show is written to every storage location at once: the USB drive's
shows directory when one is attached, and the local backup either way. That
way an upload survives the drive being pulled, and is already present wherever
ShowPlayer reads its list from next.

Two kinds are accepted:
	ProgramBlue — a single self-contained .shw file.
	MIDI        — a .mid/.midi file plus a matching audio file. The two are
	              renamed to share one stem, since that pairing is how
	              ShowPlayer._resolve_show finds them.

convert_to_program_blue() hands an uploaded MIDI set to the converter in
tools/ and replaces it with the .shw that comes back.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import List, Optional, Tuple

import usb_monitor
import utils
from logger import get_logger

log = get_logger(__name__)


AUDIO_EXTENSIONS = ('.mp3', '.wav', '.ogg')
MIDI_EXTENSIONS  = ('.mid', '.midi')
SHW_EXTENSION    = '.shw'

CONVERTER_SCRIPT = os.path.join("tools", "programBlue_midi_converter.py")
# The converter transcodes the whole audio track through ffmpeg, so this is a
# runaway guard rather than an expected duration.
CONVERT_TIMEOUT_S = 900

# Control characters and path separators are all that get stripped from an
# uploaded name. Spaces, ampersands and dashes are part of real show titles,
# and the matcher in voice_commands.py already folds them.
_UNSAFE_NAME_RE = re.compile(r'[\x00-\x1f\x7f/\\]')


def _safe_name(name: str) -> Optional[str]:
	"""Reduce an uploaded filename to a bare, writable name, or None."""
	cleaned = _UNSAFE_NAME_RE.sub('', os.path.basename(str(name or ''))).strip()
	if not cleaned or cleaned.startswith('.'):
		return None
	return cleaned


def _failed(message: str) -> dict:
	log.warning(f"ShowUpload: {message}")
	return {"success": False, "message": message}


def _write_file(path: str, data: bytes) -> None:
	"""Write atomically — a drive pulled mid-upload must not leave a partial
	file that the show list will happily offer."""
	tmp_path = path + ".tmp"
	with open(tmp_path, 'wb') as f:
		f.write(data)
	os.replace(tmp_path, path)


def _first_existing(directory: str, stem: str, extensions: tuple) -> Optional[str]:
	for ext in extensions:
		path = os.path.join(directory, stem + ext)
		if os.path.isfile(path):
			return path
	return None


class ShowUploader:
	def __init__(self, base_dir: str, hardware_path: Optional[str] = None) -> None:
		self._base_dir = base_dir
		self._hardware_path = hardware_path
		# Uploads arrive on the HTTP thread and conversions on their own, and
		# both rewrite the same directories.
		self._lock = threading.Lock()

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

	def destinations(self) -> List[str]:
		"""Every shows directory an upload should land in, USB first."""
		directories = []
		if usb_monitor.is_mounted():
			directories.append(os.path.join(usb_monitor.USB_MOUNT_POINT, utils.SHOWS_DIRNAME))
		directories.append(os.path.join(self._base_dir, utils.SHOWS_DIRNAME))
		return directories

	def upload(self, files: List[Tuple[str, bytes]]) -> dict:
		"""Write one uploaded show set. files is [(filename, contents)].

		Only complete sets are accepted — the web UI holds a half-chosen MIDI
		show until its partner arrives and sends both together."""
		with self._lock:
			return self._upload(files)

	def convert_to_program_blue(self, show_name: str) -> dict:
		"""Convert an already-uploaded MIDI show to a .shw and drop the
		MIDI/audio pair it came from."""
		with self._lock:
			return self._convert(show_name)

	# -------------------------------------------------------------------------
	# Internal
	# -------------------------------------------------------------------------

	def _upload(self, files: List[Tuple[str, bytes]]) -> dict:
		cleaned: List[Tuple[str, bytes]] = []
		for name, data in files or []:
			safe = _safe_name(name)
			if safe is None:
				return _failed(f"'{name}' is not a usable filename.")
			if not data:
				return _failed(f"'{safe}' is empty.")
			cleaned.append((safe, data))

		if not cleaned:
			return _failed("No files were uploaded.")

		shw   = [f for f in cleaned if f[0].lower().endswith(SHW_EXTENSION)]
		midi  = [f for f in cleaned if f[0].lower().endswith(MIDI_EXTENSIONS)]
		audio = [f for f in cleaned if f[0].lower().endswith(AUDIO_EXTENSIONS)]

		if len(shw) + len(midi) + len(audio) != len(cleaned):
			return _failed("Only .shw, MIDI and audio files can be uploaded.")

		if shw:
			if midi or audio or len(shw) > 1:
				return _failed("Upload a ProgramBlue show on its own.")
			return self._write_set(os.path.splitext(shw[0][0])[0], shw, "programblue")

		if len(midi) != 1 or len(audio) != 1:
			return _failed("A MIDI show needs exactly one MIDI file and one audio file.")

		# The audio file carries the song title; a MIDI export is usually named
		# after whatever the sequencer called the project, so the audio stem
		# wins and the MIDI is renamed to match it.
		stem = os.path.splitext(audio[0][0])[0]
		renamed = [
			(stem + os.path.splitext(midi[0][0])[1].lower(),  midi[0][1]),
			(stem + os.path.splitext(audio[0][0])[1].lower(), audio[0][1]),
		]
		return self._write_set(stem, renamed, "midi")

	def _write_set(self, show_name: str, files: List[Tuple[str, bytes]], kind: str) -> dict:
		destinations = self._write_destinations(sum(len(data) for _, data in files))
		written: List[str] = []
		errors:  List[str] = []

		for directory, allowed, reason in destinations:
			if not allowed:
				errors.append(reason)
				continue
			try:
				os.makedirs(directory, exist_ok=True)
				for name, data in files:
					_write_file(os.path.join(directory, name), data)
				written.append(directory)
			except OSError as e:
				errors.append(f"could not write to '{directory}': {e}")

		if not written:
			return _failed("Could not write the show anywhere — " + "; ".join(errors))

		message = f"Uploaded '{show_name}' to {len(written)} location(s)."
		result = {"success": True, "show": show_name, "kind": kind, "message": message}
		if errors:
			result["warning"] = "; ".join(errors)
			log.info(f"ShowUpload: {message} ({result['warning']})")
		else:
			log.info(f"ShowUpload: {message}")
		return result

	def _write_destinations(self, total_bytes: int) -> List[Tuple[str, bool, str]]:
		"""Decide which directories this upload may use, as
		(directory, allowed, reason-if-not)."""
		directories = self.destinations()
		local_dir = os.path.join(self._base_dir, utils.SHOWS_DIRNAME)
		decided: List[Tuple[str, bool, str]] = []

		for directory in directories:
			if (directory == local_dir and len(directories) > 1
					and utils.directory_size(directory) + total_bytes > utils.MAX_LOCAL_SHOWS_BYTES):
				decided.append((directory, False,
					f"skipped the local backup — it would go past "
					f"{utils.MAX_LOCAL_SHOWS_BYTES // (1024 * 1024)}MB"))
				continue
			decided.append((directory, True, ""))
		return decided

	def _convert(self, show_name: str) -> dict:
		show_name = _safe_name(show_name)
		if not show_name:
			return _failed("No show name given to convert.")
		if not self._hardware_path or not os.path.isfile(self._hardware_path):
			return _failed("No character config available for the converter.")

		converter = os.path.join(self._base_dir, CONVERTER_SCRIPT)
		if not os.path.isfile(converter):
			return _failed(f"Converter not found at '{converter}'.")

		destinations = self.destinations()
		found = self._find_midi_set(destinations, show_name)
		if found is None:
			return _failed(f"No MIDI show named '{show_name}' to convert.")
		midi_path, audio_path = found

		# Converted in a scratch directory: the converter writes its output
		# beside its input, and those intermediates should never touch the USB
		# drive or the backup.
		with tempfile.TemporaryDirectory(prefix="show_convert_") as work_dir:
			work_midi = os.path.join(work_dir, os.path.basename(midi_path))
			try:
				shutil.copyfile(midi_path, work_midi)
				shutil.copyfile(audio_path, os.path.join(work_dir, os.path.basename(audio_path)))
			except OSError as e:
				return _failed(f"Could not stage '{show_name}' for conversion: {e}")

			log.info(f"ShowUpload: converting '{show_name}' to ProgramBlue...")
			try:
				result = subprocess.run(
					[sys.executable, converter, work_midi, self._hardware_path],
					capture_output=True, text=True, timeout=CONVERT_TIMEOUT_S,
				)
			except subprocess.TimeoutExpired:
				return _failed(f"Converting '{show_name}' timed out.")
			except OSError as e:
				return _failed(f"Could not run the converter: {e}")

			if result.returncode != 0:
				detail = (result.stderr or result.stdout or "").strip().splitlines()
				return _failed(f"Converter failed: {detail[-1] if detail else 'unknown error'}")

			shw_path = os.path.splitext(work_midi)[0] + SHW_EXTENSION
			if not os.path.isfile(shw_path):
				return _failed("The converter did not produce a .shw file.")
			with open(shw_path, 'rb') as f:
				shw_data = f.read()

		written = self._write_set(show_name, [(show_name + SHW_EXTENSION, shw_data)], "programblue")
		if not written.get("success"):
			return written

		# Only now that the .shw is safely on disk.
		removed = self._remove_midi_set(destinations, show_name)
		written["message"] = f"Converted '{show_name}' to a ProgramBlue show."
		log.info(f"ShowUpload: {written['message']} Removed {removed} MIDI/audio file(s).")
		return written

	def _find_midi_set(self, destinations: List[str], show_name: str) -> Optional[Tuple[str, str]]:
		"""First directory holding both halves of the named MIDI show wins."""
		for directory in destinations:
			midi_path = _first_existing(directory, show_name, MIDI_EXTENSIONS)
			audio_path = _first_existing(directory, show_name, AUDIO_EXTENSIONS)
			if midi_path and audio_path:
				return midi_path, audio_path
		return None

	def _remove_midi_set(self, destinations: List[str], show_name: str) -> int:
		removed = 0
		for directory in destinations:
			for ext in MIDI_EXTENSIONS + AUDIO_EXTENSIONS:
				path = os.path.join(directory, show_name + ext)
				if not os.path.isfile(path):
					continue
				try:
					os.remove(path)
					removed += 1
				except OSError as e:
					log.exception(f"ShowUpload: could not remove '{path}': {e}")
		return removed
