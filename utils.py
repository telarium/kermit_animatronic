"""Shared helper functions for the animatronic project.

Currently holds the config-file (INI) read/write helpers used by the
web-based config editor: reading config.cfg into a dict for broadcast
to the webpage, and writing edited values back without disturbing the
file's comments and formatting.
"""

import os
import re
import configparser


# Matches a "Key = value" line, capturing indent, key, separator, and value.
_KEY_LINE_RE = re.compile(r'^(\s*)([^#;=\s][^=]*?)(\s*=\s*)(.*)$')
# Matches a "[Section]" header line.
_SECTION_RE = re.compile(r'^\s*\[(.+?)\]\s*$')


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
