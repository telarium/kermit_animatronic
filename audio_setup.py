"""
audio_setup.py — PCM5102 I2S DAC bring-up on the Jetson Orin Nano APE card.

Playback for the animatronic runs through the on-board APE sound card driving a PCM5102
I2S DAC on the 40-pin header (BCLK=pin12, WSEL=pin35, DIN=pin40). Two things
must be true at runtime before any audio will play:

  1. The device-tree overlay that muxes pins 12/35/40 to i2s2 must be applied.
     This lives in /boot and persists across reboots — BUT re-running
     `jetson-io` regenerates a BROKEN version (wrong tristate + a spurious
     gpio-mode property) that leaves the pads undriven. The known-good overlay
     source is committed at lib/pcm5102/nvidia_jetson/i2s-overlay.dts. If audio
     is silent after someone touched jetson-io, recompile and reinstall that
     .dts (see apply_overlay_from_source below) and reboot.

  2. The AHUB crossbar must route I2S2 <- ADMAIF1. This does NOT survive a
     reboot, so setup_i2s_routing() must run at every startup, before playback.

Microphone capture is not here — it lives in mic_stream.py.

find_ape_audio_card() returns the ALSA device string for pygame's AUDIODEV.
We use the card *name* ("APE") rather than a numeric index because the index
can shift between boots; plughw: also gives us free sample-rate conversion so
22.05k/24k TTS output plays fine over the 48k I2S link.
"""

import io
import subprocess
import threading
import time
import wave
from typing import Optional

# ALSA control name for the AHUB crossbar mux feeding the header I2S port.
# On the Orin Nano 40-pin header, the enabled port is I2S2 (see jetson-io).
_I2S_MUX_CONTROL = "I2S2 Mux"
_I2S_MUX_SOURCE = "ADMAIF1"
_APE_CARD_NAME = "APE"

# --- DAC wake state ---------------------------------------------------------
# The PCM5102 drops its output stage when the I2S link goes idle, so the first
# fraction of a second after a silent gap gets swallowed. The fix is to push a
# brief inaudible tone through first and let the DAC settle before real audio
# starts.
#
# This state lives here rather than on VoicePlayer or ShowPlayer because there
# is only ONE DAC. If each player tracked its own idle time they would never
# see each other's playback, and a show starting straight after the animatronic speaks
# would fire a redundant wake tone — 150ms of dead air before every show that
# follows a spoken line, which is the common case.
_dac_lock = threading.Lock()
_last_play_time: float = 0.0

# Seconds of silence after which the DAC is assumed to have dropped out.
DAC_WAKE_THRESHOLD = 2.0
# Duration and frequency of the wake tone. 80Hz at 4% amplitude is below the
# threshold of notice through the puppet's speaker but is enough signal to
# bring the output stage up.
_WAKE_SECONDS = 0.15
_WAKE_HZ = 80
_WAKE_AMPLITUDE = 0.04
# Must match pygame.mixer.pre_init() in start.py.
_WAKE_RATE = 44100


def _build_wake_tone() -> io.BytesIO:
	"""Generate the wake tone as an in-memory WAV.

	Written with the stdlib wave module rather than scipy so that importing
	audio_setup stays cheap — start.py imports it before the mixer exists.
	"""
	import numpy as np

	samples = int(_WAKE_RATE * _WAKE_SECONDS)
	t = np.linspace(0, _WAKE_SECONDS, samples, endpoint=False)
	tone = (np.sin(2 * np.pi * _WAKE_HZ * t) * 32767 * _WAKE_AMPLITUDE).astype(np.int16)
	stereo = np.column_stack([tone, tone])

	buf = io.BytesIO()
	with wave.open(buf, "wb") as wf:
		wf.setnchannels(2)
		wf.setsampwidth(2)
		wf.setframerate(_WAKE_RATE)
		wf.writeframes(stereo.tobytes())
	buf.seek(0)
	return buf


def wake_dac_if_needed(pygame_instance) -> None:
	"""Wake the DAC if it has been idle long enough to have dropped out.

	Call this immediately before starting playback, and call note_playback()
	when that playback finishes. Blocks for ~150ms when a wake is actually
	needed and returns immediately otherwise.

	Note for timed shows: this deliberately plays a SEPARATE tone rather than
	padding the show audio itself. ShowPlayer drives its animation events off
	mixer.music.get_pos(), so prepending silence to the audio stream would
	shift every event later by that amount.
	"""
	global _last_play_time
	with _dac_lock:
		idle = time.monotonic() - _last_play_time
		if idle > DAC_WAKE_THRESHOLD:
			try:
				pygame_instance.mixer.Sound(_build_wake_tone()).play()
				# Let the tone finish before the caller loads real audio,
				# otherwise the two overlap and the wake is wasted.
				time.sleep(_WAKE_SECONDS + 0.01)
			except Exception as e:
				print(f"Audio: DAC wake failed (continuing anyway): {e}")
		_last_play_time = time.monotonic()


def note_playback() -> None:
	"""Record that audio just finished, so the next caller can judge idle time."""
	global _last_play_time
	with _dac_lock:
		_last_play_time = time.monotonic()


# Playback device string. plughw (not hw) so ALSA converts sample rate/format
# from whatever the source is up to what the I2S link runs.
_APE_PLAYBACK_DEVICE = "plughw:APE,0"


def find_ape_audio_card() -> Optional[str]:
	"""Return the APE playback device string if the APE card is present.

	Scans `aplay -l` for the APE card. Returns 'plughw:APE,0' (by name, which
	is stable across boots) or None if the card isn't found — which almost
	always means the I2S overlay didn't apply and the sound card never
	registered.
	"""
	try:
		result = subprocess.run(["aplay", "-l"], capture_output=True, text=True)
		for line in result.stdout.splitlines():
			# e.g. "card 1: APE [NVIDIA Jetson Orin Nano APE], device 0: ..."
			if "APE" in line and line.strip().lower().startswith("card"):
				print(f"Audio: found APE card -> {_APE_PLAYBACK_DEVICE}")
				return _APE_PLAYBACK_DEVICE
	except Exception as e:
		print(f"Audio: error scanning for APE card: {e}")
	print("Audio: APE card not found (is the I2S overlay applied?).")
	return None


def setup_i2s_routing() -> bool:
	"""Route the AHUB crossbar I2S2 <- ADMAIF1 and verify it took.

	This must run at every startup — the route does not persist across reboots.
	Returns True on verified success, False otherwise. Does not raise, so a
	routing failure degrades to 'no audio' with a clear log line rather than
	killing startup.
	"""
	try:
		subprocess.run(
			["amixer", "-c", _APE_CARD_NAME, "cset",
			 f"name={_I2S_MUX_CONTROL}", _I2S_MUX_SOURCE],
			check=True, capture_output=True, text=True,
		)
	except FileNotFoundError:
		print("Audio: 'amixer' not found — is alsa-utils installed?")
		return False
	except subprocess.CalledProcessError as e:
		print(f"Audio: failed to set '{_I2S_MUX_CONTROL}': {e.stderr.strip()}")
		return False

	if verify_i2s_routing():
		print(f"Audio: routed {_I2S_MUX_CONTROL} <- {_I2S_MUX_SOURCE}")
		return True

	print(f"Audio: '{_I2S_MUX_CONTROL}' did not read back as {_I2S_MUX_SOURCE}.")
	return False


def verify_i2s_routing() -> bool:
	"""Read back the I2S2 Mux and confirm it points at ADMAIF1.

	The enum reports the selected item index on the ': values=N' line. ADMAIF1
	is item #1 in the I2S2 Mux enumeration, so we look for 'values=1'. We also
	confirm the source name appears in the control dump as a guard against the
	item ordering differing on some L4T revision.
	"""
	try:
		result = subprocess.run(
			["amixer", "-c", _APE_CARD_NAME, "cget", f"name={_I2S_MUX_CONTROL}"],
			capture_output=True, text=True,
		)
	except Exception as e:
		print(f"Audio: error reading back routing: {e}")
		return False

	out = result.stdout
	# Item #1 is ADMAIF1 in this enum; the active selection is the last
	# ': values=N' line.
	selected_one = any(
		line.strip().startswith(": values=") and line.strip().endswith("=1")
		for line in out.splitlines()
	)
	names_ok = _I2S_MUX_SOURCE in out
	return selected_one and names_ok


def init_audio() -> Optional[str]:
	"""Full playback bring-up: route the AHUB, then locate the APE device.

	Returns the playback device string for pygame's AUDIODEV, or None if the
	card can't be found. Call once at startup before pygame.mixer.init().
	"""
	setup_i2s_routing()
	return find_ape_audio_card()
