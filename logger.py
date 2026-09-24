"""Central logging for the animatronic.

Every module logs through `get_logger(__name__)`; start.py calls
`setup_logging()` once, before anything else is imported.

Where the records go:

  * stdout, immediately — under systemd that is the journal.
  * A buffer in memory, flushed to <base_dir>/logs/animatronic.log every
    30 seconds, and immediately on anything at WARNING or above. Errors are
    never left sitting in RAM, which matters when the way this machine shuts
    down is someone pulling the power.
  * The same file on the USB drive, while a drive is mounted.

Both files are rotated at 5MB with one old copy kept, so each location holds
at most 10MB and old entries are pruned by rotation rather than by rewriting
a file in place.

Nothing that looks like a credential reaches any of them — see RedactionFilter.
"""

import logging
import logging.handlers
import os
import re
import shutil
import sys
import threading
import time
from typing import Optional
from pydispatch import dispatcher

LOG_DIRNAME = "logs"
LOG_FILENAME = "animatronic.log"

# 5MB x (1 current + 1 backup) = the 10MB ceiling, per location.
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 1

FLUSH_INTERVAL_S = 30.0
# Records held in memory between flushes. A burst longer than this flushes
# early rather than growing without bound.
BUFFER_CAPACITY = 400
# Anything this severe is written out immediately.
FLUSH_LEVEL = logging.WARNING

USB_MOUNT_POINT = "/mnt/usb"

# A wall-clock jump larger than this is reported, so a remote reader can tell
# an NTP correction from a gap in the log. There is no RTC on this board:
# everything logged before the first sync carries whatever date the system
# came up with.
CLOCK_STEP_TOLERANCE_S = 3.0
CLOCK_POLL_INTERVAL_S = 5.0

_START_MONOTONIC = time.monotonic()

_setup_lock = threading.Lock()
_configured = False
_memory_handler: Optional[logging.handlers.MemoryHandler] = None
_fanout: Optional["_Fanout"] = None
_usb_mount_point = USB_MOUNT_POINT


# -----------------------------------------------------------------------------
# Redaction
# -----------------------------------------------------------------------------

_secrets_lock = threading.Lock()
_secrets: set = set()

# Values shorter than this are too likely to appear in ordinary text to
# blanket-replace.
_MIN_SECRET_LEN = 8

_REDACTED = "<redacted>"

_PATTERNS = (
	# Provider key formats.
	re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"),
	re.compile(r"\bxi-[A-Za-z0-9_\-]{12,}"),
	# Long hex blobs — ElevenLabs keys and similar.
	re.compile(r"\b[0-9a-fA-F]{32,}\b"),
	# key = value / "password": value, however it was written.
	re.compile(
		r"(?i)\b(api[_\-]?key|apikey|access[_\-]?token|token|secret|password|passwd|psk)\b"
		r"(\s*[:=]\s*|\"\s*:\s*)(\"?)([^\s\"',}]+)"
	),
)


def register_secret(value: str) -> None:
	"""Register a value that must never appear in the log.

	Call this wherever a credential is read from the config. The patterns
	above are a safety net for keys this never sees; this is the reliable
	half, because it matches the exact string regardless of its shape."""
	if not value or len(value) < _MIN_SECRET_LEN:
		return
	with _secrets_lock:
		_secrets.add(value)


def clear_secrets() -> None:
	"""Forget registered secrets — used when a config is reloaded."""
	with _secrets_lock:
		_secrets.clear()


def _redact(text: str) -> str:
	with _secrets_lock:
		secrets = tuple(_secrets)
	for secret in secrets:
		if secret in text:
			text = text.replace(secret, _REDACTED)
	for pattern in _PATTERNS:
		if pattern.groups >= 4:
			text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{_REDACTED}", text)
		else:
			text = pattern.sub(_REDACTED, text)
	return text


class RedactionFilter(logging.Filter):
	"""Render the record and strip anything credential-shaped out of it.

	Rendering here rather than at each handler means the text is scrubbed
	once, before it is buffered, so a secret is never held in memory in a
	record waiting to be flushed."""

	def filter(self, record: logging.LogRecord) -> bool:
		try:
			message = record.getMessage()
		except Exception:
			message = str(record.msg)
		record.msg = _redact(message)
		record.args = ()
		if record.exc_text:
			record.exc_text = _redact(record.exc_text)
		return True


class _UptimeFilter(logging.Filter):
	"""Add seconds-since-start, which stays monotonic across clock steps."""

	def filter(self, record: logging.LogRecord) -> bool:
		record.uptime = time.monotonic() - _START_MONOTONIC
		return True


# -----------------------------------------------------------------------------
# Handlers
# -----------------------------------------------------------------------------

class _Fanout(logging.Handler):
	"""Writes each record to every attached target handler.

	The memory buffer has one target, so this is what lets the USB file come
	and go underneath it without disturbing the buffer."""

	def __init__(self) -> None:
		super().__init__()
		self._targets: list = []
		self._targets_lock = threading.Lock()

	def add_target(self, handler: logging.Handler) -> None:
		with self._targets_lock:
			self._targets.append(handler)

	def remove_target(self, handler: logging.Handler) -> None:
		with self._targets_lock:
			if handler in self._targets:
				self._targets.remove(handler)
		try:
			handler.close()
		except Exception:
			pass

	def targets(self) -> list:
		with self._targets_lock:
			return list(self._targets)

	def emit(self, record: logging.LogRecord) -> None:
		for handler in self.targets():
			try:
				handler.handle(record)
			except Exception:
				pass

	def flush(self) -> None:
		for handler in self.targets():
			try:
				handler.flush()
			except Exception:
				pass


class _SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
	"""A rotating file handler that detaches itself instead of raising.

	The USB copy writes to a drive that can be unplugged mid-write, and a
	write error there must never reach the caller — losing the USB copy is
	acceptable, taking down the thread that was logging is not."""

	MAX_FAILURES = 3

	def __init__(self, *args, on_failure=None, **kwargs) -> None:
		super().__init__(*args, **kwargs)
		self._failures = 0
		self._on_failure = on_failure

	def emit(self, record: logging.LogRecord) -> None:
		try:
			super().emit(record)
			self._failures = 0
		except Exception:
			self._failures += 1
			if self._failures >= self.MAX_FAILURES and self._on_failure:
				callback, self._on_failure = self._on_failure, None
				try:
					callback(self)
				except Exception:
					pass

	def handleError(self, record: logging.LogRecord) -> None:
		# Swallow it. The base class prints to stderr, which under systemd
		# lands right back in the journal as noise.
		pass


# -----------------------------------------------------------------------------
# Setup
# -----------------------------------------------------------------------------

def get_logger(name: str) -> logging.Logger:
	"""Return the logger for a module. Safe to call at import time, and
	before setup_logging — a module run on its own still prints to stdout."""
	if not _configured:
		_ensure_console_only()
	return logging.getLogger(name)


def _formatter() -> logging.Formatter:
	# The uptime column is what makes a log readable when the clock was wrong
	# for the first part of it.
	return logging.Formatter(
		fmt="%(asctime)s.%(msecs)03d  +%(uptime)8.1fs  %(levelname)-8s %(name)s: %(message)s",
		datefmt="%Y-%m-%d %H:%M:%S",
	)


def _attach_filters(handler: logging.Handler) -> None:
	handler.addFilter(_UptimeFilter())
	handler.addFilter(RedactionFilter())


def _ensure_console_only() -> None:
	"""Minimal stdout logging for a module imported or run outside start.py."""
	global _configured
	with _setup_lock:
		if _configured:
			return
		root = logging.getLogger()
		root.setLevel(logging.INFO)
		console = logging.StreamHandler(sys.stdout)
		console.setFormatter(_formatter())
		_attach_filters(console)
		root.addHandler(console)
		_configured = True


def setup_logging(base_dir: str, level: int = logging.INFO,
                  usb_mount_point: str = USB_MOUNT_POINT,
                  console: bool = True) -> str:
	"""Configure logging for the whole process. Returns the local log path.

	Call this once, first thing in start.py, before importing any module
	that logs at import time."""
	global _configured, _memory_handler, _fanout, _usb_mount_point

	with _setup_lock:
		root = logging.getLogger()
		# _ensure_console_only may have run during an early import.
		for handler in list(root.handlers):
			root.removeHandler(handler)
			try:
				handler.close()
			except Exception:
				pass

		root.setLevel(level)
		_usb_mount_point = usb_mount_point

		log_dir = os.path.join(base_dir, LOG_DIRNAME)
		log_path = os.path.join(log_dir, LOG_FILENAME)
		try:
			os.makedirs(log_dir, exist_ok=True)
		except OSError as e:
			print(f"Logger: could not create '{log_dir}': {e}", file=sys.stderr)

		_fanout = _Fanout()
		_fanout.setFormatter(_formatter())

		try:
			local_file = _SafeRotatingFileHandler(
				log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
				encoding="utf-8", delay=True,
			)
			local_file.setFormatter(_formatter())
			_fanout.add_target(local_file)
		except OSError as e:
			print(f"Logger: could not open '{log_path}': {e}", file=sys.stderr)

		_memory_handler = logging.handlers.MemoryHandler(
			capacity=BUFFER_CAPACITY, flushLevel=FLUSH_LEVEL, target=_fanout,
			flushOnClose=True,
		)
		_attach_filters(_memory_handler)
		root.addHandler(_memory_handler)

		if console:
			console_handler = logging.StreamHandler(sys.stdout)
			console_handler.setFormatter(_formatter())
			_attach_filters(console_handler)
			root.addHandler(console_handler)

		_configured = True

	threading.Thread(target=_flush_loop, daemon=True, name="log-flush").start()
	threading.Thread(target=_clock_watch, daemon=True, name="log-clock").start()

	dispatcher.connect(_on_usb_attached, signal="usbAttached")
	dispatcher.connect(_on_usb_detached, signal="usbDetached")

	# A drive mounted before this ran won't send the signal.
	if os.path.isdir(_usb_mount_point) and os.path.ismount(_usb_mount_point):
		threading.Thread(target=_attach_usb, daemon=True).start()

	return log_path


def flush() -> None:
	"""Write anything buffered out to the log files now."""
	if _memory_handler:
		try:
			_memory_handler.flush()
		except Exception:
			pass


def _flush_loop() -> None:
	while True:
		time.sleep(FLUSH_INTERVAL_S)
		if _memory_handler and _memory_handler.buffer:
			flush()


def _clock_watch() -> None:
	"""Report wall-clock steps, so timestamps before an NTP sync can be
	recognised for what they are."""
	log = logging.getLogger(__name__)
	previous = time.time() - time.monotonic()
	while True:
		time.sleep(CLOCK_POLL_INTERVAL_S)
		current = time.time() - time.monotonic()
		delta = current - previous
		if abs(delta) > CLOCK_STEP_TOLERANCE_S:
			log.warning(
				"System clock stepped by %+.1fs — entries above this line are "
				"stamped with the old clock.", delta)
			flush()
		previous = current


# -----------------------------------------------------------------------------
# USB mirror
# -----------------------------------------------------------------------------

_usb_handler: Optional[_SafeRotatingFileHandler] = None
_usb_lock = threading.Lock()


def _on_usb_attached(path: str = "", **kwargs) -> None:
	threading.Thread(target=_attach_usb, daemon=True).start()


def _on_usb_detached(**kwargs) -> None:
	_detach_usb()


def _attach_usb() -> None:
	global _usb_handler
	log = logging.getLogger(__name__)
	if _fanout is None:
		return
	with _usb_lock:
		if _usb_handler is not None:
			return
		usb_log_dir = os.path.join(_usb_mount_point, LOG_DIRNAME)
		usb_log_path = os.path.join(usb_log_dir, LOG_FILENAME)
		try:
			os.makedirs(usb_log_dir, exist_ok=True)
			_seed_usb_copy(usb_log_path)
			handler = _SafeRotatingFileHandler(
				usb_log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
				encoding="utf-8", delay=True, on_failure=_drop_usb_handler,
			)
			handler.setFormatter(_formatter())
		except Exception as e:
			log.warning("Logger: could not open the USB log at '%s': %s", usb_log_path, e)
			return
		_usb_handler = handler
		_fanout.add_target(handler)
	log.info("Logger: mirroring the log to %s", usb_log_path)
	flush()


def _seed_usb_copy(usb_log_path: str) -> None:
	"""Copy the local log across the first time a drive is attached, so the
	USB copy holds the whole session rather than starting mid-story.

	Only when the drive has nothing (or less) already: re-copying on every
	attach would wear the stick for no gain."""
	local_path = _local_log_path()
	if not local_path or not os.path.exists(local_path):
		return
	try:
		local_size = os.path.getsize(local_path)
		usb_size = os.path.getsize(usb_log_path) if os.path.exists(usb_log_path) else 0
		if usb_size >= local_size:
			return
		flush()
		shutil.copyfile(local_path, usb_log_path)
	except OSError:
		pass


def _local_log_path() -> str:
	if _fanout is None:
		return ""
	for handler in _fanout.targets():
		if isinstance(handler, logging.handlers.RotatingFileHandler):
			return handler.baseFilename
	return ""


def _drop_usb_handler(handler: logging.Handler) -> None:
	"""Called by the handler itself after repeated write failures."""
	global _usb_handler
	with _usb_lock:
		if _usb_handler is handler:
			_usb_handler = None
	if _fanout:
		_fanout.remove_target(handler)
	logging.getLogger(__name__).warning(
		"Logger: USB log writes kept failing — dropping the USB copy.")


def _detach_usb() -> None:
	global _usb_handler
	with _usb_lock:
		handler, _usb_handler = _usb_handler, None
	if handler is None:
		return
	if _fanout:
		_fanout.remove_target(handler)
	logging.getLogger(__name__).info("Logger: USB drive gone — local log only.")


# -----------------------------------------------------------------------------
# Boot banner
# -----------------------------------------------------------------------------

def log_boot_banner(base_dir: str, **fields) -> None:
	"""Record what this process actually is. Most remote diagnosis starts
	with working out which build and which config was running."""
	log = logging.getLogger("boot")
	log.info("=" * 60)
	log.info("Animatronic starting")
	log.info("  python     : %s", sys.version.split()[0])
	log.info("  base dir   : %s", base_dir)
	log.info("  host       : %s", _read_command(["hostname"]) or "unknown")
	log.info("  kernel     : %s", _read_command(["uname", "-r"]) or "unknown")
	log.info("  git        : %s", _git_description(base_dir) or "not a git checkout")
	for name, value in fields.items():
		log.info("  %-10s : %s", name.replace("_", " "), value)
	log.info("=" * 60)
	flush()


def _read_command(args: list) -> str:
	import subprocess
	try:
		result = subprocess.run(args, capture_output=True, text=True, timeout=5)
		return result.stdout.strip()
	except Exception:
		return ""


def _git_description(base_dir: str) -> str:
	import subprocess
	try:
		result = subprocess.run(
			["git", "-C", base_dir, "describe", "--always", "--dirty", "--tags"],
			capture_output=True, text=True, timeout=5,
		)
		if result.returncode == 0:
			return result.stdout.strip()
	except Exception:
		pass
	return ""
