"""Shared helper functions for the animatronic project.

Currently holds the config-file (INI) read/write helpers used by the
web-based config editor: reading config.cfg into a dict for broadcast
to the webpage, and writing edited values back without disturbing the
file's comments and formatting.
"""

import os
import re
import configparser


CONFIG_FILENAME = "config.cfg"
TEMPLATE_FILENAME = "config_template.cfg"


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


def resolve_config(base_dir: str, usb_mount_point: str, usb_mounted: bool, usb_config_path: str = "") -> str:
	"""Decide which config file to use, and keep the local/USB copies in sync.

	Priority:
	1. A valid config on the USB drive — used, and backed up to the local
	   directory as a validated known-good copy.
	2. A valid local config — used; if a USB drive is attached but has no
	   config, the local one is copied onto it.
	3. Neither — bootstrap a fresh config from config_template.cfg into the
	   local directory and onto the USB drive (if attached).

	usb_config_path allows the USB monitor to pass an explicitly discovered
	.cfg path (which may not be named config.cfg). Returns the path to load,
	or None if no config could be found or created."""
	local_cfg = os.path.join(base_dir, CONFIG_FILENAME)
	template = os.path.join(base_dir, TEMPLATE_FILENAME)
	default_usb_cfg = os.path.join(usb_mount_point, CONFIG_FILENAME)
	usb_cfg = usb_config_path or default_usb_cfg

	# 1. Valid USB config wins; mirror it locally as a known-good backup.
	if usb_mounted and os.path.exists(usb_cfg):
		valid, err = validate_config(usb_cfg)
		if valid:
			try:
				if copy_file_if_different(usb_cfg, local_cfg):
					print(f"Config: backed up USB config to {local_cfg}")
			except OSError as e:
				print(f"Config: could not back up USB config locally: {e}")
			return usb_cfg
		print(f"Config: USB config '{usb_cfg}' is invalid ({err}); ignoring it.")

	# 2. Valid local config; seed the USB drive if it has none.
	if os.path.exists(local_cfg):
		valid, err = validate_config(local_cfg)
		if valid:
			if usb_mounted and not os.path.exists(default_usb_cfg):
				try:
					copy_file_if_different(local_cfg, default_usb_cfg)
					print(f"Config: copied local config to USB drive at {default_usb_cfg}")
				except OSError as e:
					print(f"Config: could not copy config to USB drive: {e}")
			return local_cfg
		print(f"Config: local config '{local_cfg}' is invalid ({err}).")
		# Preserve the broken file for inspection before the template replaces it.
		try:
			os.replace(local_cfg, local_cfg + ".invalid")
			print(f"Config: moved invalid config to {local_cfg}.invalid")
		except OSError as e:
			print(f"Config: could not move invalid config aside: {e}")

	# 3. Bootstrap fresh configs from the template.
	if not os.path.exists(template):
		print(f"Config: template '{template}' not found; cannot create a config.")
		return None
	result = None
	try:
		copy_file_if_different(template, local_cfg)
		print(f"Config: created {local_cfg} from template.")
		result = local_cfg
	except OSError as e:
		print(f"Config: could not create local config from template: {e}")
	if usb_mounted and not os.path.exists(default_usb_cfg):
		try:
			copy_file_if_different(template, default_usb_cfg)
			print(f"Config: created {default_usb_cfg} from template.")
			if result is None:
				result = default_usb_cfg
		except OSError as e:
			print(f"Config: could not create USB config from template: {e}")
	return result


def sync_config_copies(active_path: str, base_dir: str, usb_mount_point: str, usb_mounted: bool) -> list:
	"""Mirror the active config file to the other standard location(s):
	the local directory, and the USB drive if attached. Used after a save
	so both copies stay identical. Returns a list of error strings."""
	errors = []
	active_abs = os.path.abspath(active_path)
	targets = []

	local_cfg = os.path.join(base_dir, CONFIG_FILENAME)
	if active_abs != os.path.abspath(local_cfg):
		targets.append(local_cfg)

	if usb_mounted:
		usb_root = os.path.abspath(usb_mount_point) + os.sep
		# If the active config already lives on the USB drive (possibly under
		# another filename), don't write a second copy next to it.
		if not active_abs.startswith(usb_root):
			targets.append(os.path.join(usb_mount_point, CONFIG_FILENAME))

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
