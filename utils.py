"""Shared helper functions for the animatronic project.

Holds the config-file (INI) read/write helpers used by the web-based config
editor, and the USB/local storage resolution used at startup and whenever a
drive is plugged in.

Storage rule: a USB drive carrying a valid config is the source of truth for
both the config and the shows, and everything on it is backed up to the local
directory next to start.py. With no drive attached, that local backup is used
instead. A drive attached WITHOUT a config is restored from the backup and
then becomes the source of truth.
"""

import glob
import os
import re
import configparser


CONFIG_FILENAME = "config.cfg"
TEMPLATE_FILENAME = "config_template.cfg"
SHOWS_DIRNAME = "shows"

# Ceiling on the local shows backup. The SSD also holds the models and the
# llama.cpp build, so the backup is not allowed to grow without bound.
MAX_LOCAL_SHOWS_BYTES = 1024 * 1024 * 1024

# Copied to the USB root when a drive is restored from the local backup.
DOCUMENT_EXTENSIONS = (".doc", ".docx")


# Matches a "Key = value" line, capturing indent, key, separator, and value.
_KEY_LINE_RE = re.compile(r'^(\s*)([^#;=\s][^=]*?)(\s*=\s*)(.*)$')
# Matches a "[Section]" header line.
_SECTION_RE = re.compile(r'^\s*\[(.+?)\]\s*$')


def validate_config(path: str) -> tuple:
	"""Check that a config file parses and has the minimum required content.
	Returns (True, "") if usable, or (False, reason)."""
	cfg = configparser.ConfigParser()
	try:
		read_ok = cfg.read(path)
		if not read_ok:
			return False, "file could not be read"
	except Exception as e:
		return False, str(e)
	# [Hardware] config is the one entry startup cannot proceed without.
	if not cfg.get("Hardware", "config", fallback="").strip():
		return False, "missing 'config' entry under [Hardware]"
	return True, ""


def copy_file_if_different(src: str, dst: str) -> bool:
	"""Copy src to dst atomically, but only if contents differ (spares
	flash/USB writes and file mtimes). Returns True if a copy happened.
	Raises OSError on failure."""
	with open(src, 'rb') as f:
		src_bytes = f.read()
	try:
		with open(dst, 'rb') as f:
			if f.read() == src_bytes:
				return False
	except OSError:
		pass  # dst missing or unreadable — proceed with the copy
	tmp_path = dst + ".tmp"
	with open(tmp_path, 'wb') as f:
		f.write(src_bytes)
	os.replace(tmp_path, dst)
	return True


def find_usb_config(usb_mount_point: str, usb_config_path: str = "") -> str:
	"""Return the path of a valid config at the USB root, or None.

	config.cfg is preferred, but any .cfg on the drive is accepted, since the
	USB monitor reports whatever it finds and the file is not always named
	config.cfg. usb_config_path is that explicitly discovered path."""
	candidates = []
	if usb_config_path:
		candidates.append(usb_config_path)
	candidates.append(os.path.join(usb_mount_point, CONFIG_FILENAME))
	candidates.extend(sorted(glob.glob(os.path.join(usb_mount_point, "*.cfg"))))

	seen = set()
	for path in candidates:
		abs_path = os.path.abspath(path)
		if abs_path in seen or not os.path.exists(path):
			continue
		seen.add(abs_path)
		valid, err = validate_config(path)
		if valid:
			return path
		print(f"Config: USB config '{path}' is invalid ({err}); ignoring it.")
	return None


def ensure_local_config(base_dir: str) -> str:
	"""Return a valid local config path, bootstrapping from the template if
	the file is missing or unusable. Returns None if neither is possible."""
	local_cfg = os.path.join(base_dir, CONFIG_FILENAME)
	template = os.path.join(base_dir, TEMPLATE_FILENAME)

	if os.path.exists(local_cfg):
		valid, err = validate_config(local_cfg)
		if valid:
			return local_cfg
		print(f"Config: local config '{local_cfg}' is invalid ({err}).")
		# Preserve the broken file for inspection before the template replaces it.
		try:
			os.replace(local_cfg, local_cfg + ".invalid")
			print(f"Config: moved invalid config to {local_cfg}.invalid")
		except OSError as e:
			print(f"Config: could not move invalid config aside: {e}")

	if not os.path.exists(template):
		print(f"Config: template '{template}' not found; cannot create a config.")
		return None
	try:
		copy_file_if_different(template, local_cfg)
	except OSError as e:
		print(f"Config: could not create local config from template: {e}")
		return None
	print(f"Config: created {local_cfg} from template.")
	return local_cfg


def _directory_size(path: str) -> int:
	"""Total size in bytes of the files directly inside path."""
	total = 0
	try:
		for name in os.listdir(path):
			file_path = os.path.join(path, name)
			if os.path.isfile(file_path):
				total += os.path.getsize(file_path)
	except OSError:
		pass
	return total


def copy_new_files(src_dir: str, dst_dir: str, max_bytes: int = None) -> int:
	"""Copy files from src_dir that dst_dir doesn't already have, returning
	the number copied. Existing files are never overwritten — this is a
	backup, not a mirror, so nothing already saved is disturbed.

	max_bytes caps the total size of dst_dir; files that would push it over
	are skipped rather than truncating the copy at the first one that
	doesn't fit."""
	if not os.path.isdir(src_dir):
		return 0
	try:
		os.makedirs(dst_dir, exist_ok=True)
	except OSError as e:
		print(f"Shows: could not create '{dst_dir}': {e}")
		return 0

	used = _directory_size(dst_dir) if max_bytes is not None else 0
	copied = 0
	for name in sorted(os.listdir(src_dir)):
		src = os.path.join(src_dir, name)
		if not os.path.isfile(src):
			continue
		dst = os.path.join(dst_dir, name)
		if os.path.exists(dst):
			continue
		try:
			size = os.path.getsize(src)
		except OSError:
			continue
		if max_bytes is not None and used + size > max_bytes:
			print(f"Shows: skipping '{name}' — would take the backup past "
			      f"{max_bytes // (1024 * 1024)}MB.")
			continue
		try:
			copy_file_if_different(src, dst)
		except OSError as e:
			print(f"Shows: could not copy '{name}': {e}")
			continue
		used += size
		copied += 1
	return copied


def copy_documents(src_dir: str, dst_dir: str) -> int:
	"""Copy any Word documents sitting in src_dir to dst_dir."""
	copied = 0
	try:
		names = sorted(os.listdir(src_dir))
	except OSError:
		return 0
	for name in names:
		if not name.lower().endswith(DOCUMENT_EXTENSIONS):
			continue
		src = os.path.join(src_dir, name)
		if not os.path.isfile(src):
			continue
		try:
			copy_file_if_different(src, os.path.join(dst_dir, name))
		except OSError as e:
			print(f"Restore: could not copy '{name}': {e}")
			continue
		copied += 1
	return copied


def resolve_storage(base_dir: str, usb_mount_point: str, usb_mounted: bool, usb_config_path: str = "") -> tuple:
	"""Decide where the config and the shows live, and refresh the backup.

	A USB drive carrying a valid config is the source of truth for both, and
	its config and shows are backed up locally. Anything else — no drive, or
	a drive with no usable config — falls back to the local backup. Nothing
	is ever written to the drive here; that only happens on an explicit
	restore (see restore_backup_to_usb).

	Returns (config_path, shows_dir, using_usb). config_path is None if no
	config could be found or created."""
	local_shows = os.path.join(base_dir, SHOWS_DIRNAME)
	try:
		os.makedirs(local_shows, exist_ok=True)
	except OSError as e:
		print(f"Shows: could not create '{local_shows}': {e}")

	usb_cfg = find_usb_config(usb_mount_point, usb_config_path) if usb_mounted else None
	if not usb_cfg:
		return ensure_local_config(base_dir), local_shows, False

	# USB is the source of truth — back it up locally.
	try:
		if copy_file_if_different(usb_cfg, os.path.join(base_dir, CONFIG_FILENAME)):
			print(f"Config: backed up USB config to {base_dir}")
	except OSError as e:
		print(f"Config: could not back up USB config locally: {e}")

	usb_shows = os.path.join(usb_mount_point, SHOWS_DIRNAME)
	copied = copy_new_files(usb_shows, local_shows, MAX_LOCAL_SHOWS_BYTES)
	if copied:
		print(f"Shows: backed up {copied} show file(s) from the USB drive.")

	return usb_cfg, usb_shows, True


def restore_backup_to_usb(base_dir: str, usb_mount_point: str, usb_mounted: bool) -> tuple:
	"""Write the local backup — config, shows, and any Word documents — onto
	an attached USB drive. Triggered from the web UI only.

	Shows already on the drive are left alone. Returns (success, message)."""
	if not usb_mounted:
		return False, "No USB drive is attached."

	local_cfg = ensure_local_config(base_dir)
	if not local_cfg:
		return False, "No local config to restore from."

	usb_cfg = os.path.join(usb_mount_point, CONFIG_FILENAME)
	try:
		copy_file_if_different(local_cfg, usb_cfg)
	except OSError as e:
		return False, f"Could not write the config to the USB drive: {e}"

	local_shows = os.path.join(base_dir, SHOWS_DIRNAME)
	shows = copy_new_files(local_shows, os.path.join(usb_mount_point, SHOWS_DIRNAME))
	documents = copy_documents(base_dir, usb_mount_point)

	message = (f"Restored the config, {shows} show file(s) and "
	           f"{documents} document(s) to the USB drive.")
	print(f"Restore: {message}")
	return True, message


def sync_config_copies(active_path: str, base_dir: str, usb_mount_point: str, usb_mounted: bool) -> list:
	"""Mirror the active config file to the other standard location(s): the
	local backup, and the config already on the USB drive. Used after a save
	so both copies stay identical. A drive with no config of its own is left
	alone — that is what the restore button is for. Returns a list of error
	strings."""
	errors = []
	active_abs = os.path.abspath(active_path)
	targets = []

	local_cfg = os.path.join(base_dir, CONFIG_FILENAME)
	if active_abs != os.path.abspath(local_cfg):
		targets.append(local_cfg)

	if usb_mounted:
		usb_cfg = find_usb_config(usb_mount_point)
		# If the active config already lives on the USB drive (possibly under
		# another filename), don't write a second copy next to it.
		if usb_cfg and active_abs != os.path.abspath(usb_cfg):
			targets.append(usb_cfg)

	for dst in targets:
		try:
			copy_file_if_different(active_path, dst)
		except OSError as e:
			errors.append(f"could not copy config to {dst}: {e}")
	return errors


def build_config_data(path: str, excluded_sections: tuple = ()) -> dict:
	"""Parse an INI config file into {section: {key: value}}.

	Key casing is preserved as written in the file. Sections whose
	lowercased name appears in excluded_sections are omitted.
	Returns {} if the file cannot be parsed."""
	cfg = configparser.ConfigParser()
	cfg.optionxform = str  # preserve key case for display
	try:
		cfg.read(path)
	except configparser.Error as e:
		print(f"Config: failed to parse '{path}': {e}")
		return {}

	data: dict = {}
	for section in cfg.sections():
		if section.strip().lower() in excluded_sections:
			continue
		data[section] = {}
		for key, value in cfg.items(section):
			data[section][key] = value
	return data


def write_config_values(path: str, updates: dict) -> None:
	"""Update key values in an INI file in place, preserving comments,
	blank lines, key ordering, and formatting.

	updates is {section: {key: value}}. Existing keys have only their value
	portion replaced; keys not present are appended at the end of their
	section; unknown sections are appended at the end of the file.
	Raises on I/O errors — callers should catch and report."""

	def split_line_end(line: str) -> tuple:
		"""Split a line into (content, line_ending)."""
		end = ''
		if line.endswith('\n'):
			line, end = line[:-1], '\n'
		if line.endswith('\r'):
			line, end = line[:-1], '\r' + end
		return line, end

	def clean(s: any) -> str:
		# Values/keys arrive from the browser — never let them inject new
		# lines (which would create rogue keys or sections in the file).
		return str(s).replace('\r', ' ').replace('\n', ' ')

	# Normalize updates for case-insensitive matching (ConfigParser reads
	# sections case-sensitively but keys case-insensitively; we match both
	# loosely and preserve whatever casing the file already uses).
	pending: dict = {}
	for section, kv in updates.items():
		sec_name = clean(section).strip()
		pending[sec_name.lower()] = (
			sec_name,
			{clean(k).strip().lower(): (clean(k).strip(), clean(v)) for k, v in kv.items()},
		)

	with open(path, 'r', newline='') as f:
		lines = f.readlines()

	newline = '\r\n' if any(line.endswith('\r\n') for line in lines) else '\n'

	out: list = []
	current_lower: str = ""

	def flush_pending_keys(sec_lower: str) -> None:
		"""Append any not-yet-written keys for a section we are leaving."""
		if sec_lower in pending:
			_, keys = pending[sec_lower]
			for key_lower in list(keys):
				key_name, value = keys.pop(key_lower)
				# Insert before trailing blank lines so the gap between
				# sections stays where it was.
				insert_at = len(out)
				while insert_at > 0 and out[insert_at - 1].strip() == "":
					insert_at -= 1
				out.insert(insert_at, f"{key_name} = {value}{newline}")

	for line in lines:
		content, line_end = split_line_end(line)

		sec_match = _SECTION_RE.match(content)
		if sec_match:
			flush_pending_keys(current_lower)
			current_lower = sec_match.group(1).strip().lower()
			out.append(line)
			continue

		stripped = content.lstrip()
		if stripped.startswith('#') or stripped.startswith(';'):
			out.append(line)
			continue

		key_match = _KEY_LINE_RE.match(content)
		if key_match and current_lower in pending:
			indent, key, sep, _old_value = key_match.groups()
			_, keys = pending[current_lower]
			hit = keys.pop(key.strip().lower(), None)
			if hit is not None:
				_, value = hit
				out.append(f"{indent}{key}{sep}{value}{line_end or newline}")
				continue

		out.append(line)

	flush_pending_keys(current_lower)

	# Any sections that never appeared in the file get appended at the end.
	for sec_lower, (sec_name, keys) in pending.items():
		if not keys:
			continue
		if out and out[-1].strip() != "":
			out.append(newline)
		out.append(f"[{sec_name}]{newline}")
		for key_lower in list(keys):
			key_name, value = keys.pop(key_lower)
			out.append(f"{key_name} = {value}{newline}")

	# Atomic write — a power cut mid-save shouldn't leave a truncated config.
	tmp_path = path + ".tmp"
	with open(tmp_path, 'w', newline='') as f:
		f.writelines(out)
	os.replace(tmp_path, path)
