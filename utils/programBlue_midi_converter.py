#!/usr/bin/env python3
"""
programBlue_midi_converter.py

Converts between ProgramBlue .shw show files and MIDI + audio file pairs,
using the hardware JSON config to map midi_note <-> program_blue_channel.

Usage:
	python3 programBlue_midi_converter.py <file> <config.json>

	<file> can be:
		- A .mid/.midi file  → finds matching audio in same dir → writes .shw
		- An audio file (.mp3/.wav/.ogg) → finds matching .mid/.midi → writes .shw
		- A .shw file → writes matching .mid and .wav in same dir

Dependencies:
	- mido      (pip install mido)
	- ffmpeg    (apt install ffmpeg)
"""

import json
import os
import re
import struct
import subprocess
import sys
import tempfile
from typing import Optional

import mido


# ─── Constants ────────────────────────────────────────────────────────────────

AUDIO_EXTENSIONS = ('.mp3', '.wav', '.ogg')
MIDI_EXTENSIONS  = ('.mid', '.midi')

SHW_FPS          = 40
MIDI_TEMPO       = 500000   # 120 BPM in microseconds per beat
MIDI_PPQ         = 960      # Ticks per quarter note — high res for ms accuracy

# v5 .shw format constants (mirrors program_blue.py)
FRAME_STRIDE     = 258
FRAME_BASE       = 20
NUM_SHW_CHANNELS = 256

# 33-byte repeating keystream. The v5 cipher is ADDITIVE, not XOR:
#   raw = (plain + key) & 0xFF        decode: plain = (raw - key) & 0xFF
# (XOR looks correct on 0x00 plaintext, which is most of the frame table,
# but produces carry artifacts on nonzero bytes.)
CIPHER_KEY = bytes.fromhex(
	"b5ad97cb9ec6a3d103fdeaa5c8ccb3a0"
	"d0cc8dcec198cec1b8bdbad9c0949ad8cb"
)


# ─── Config loading ────────────────────────────────────────────────────────────

def load_config(config_path: str) -> dict:
	with open(config_path, 'r') as f:
		return json.load(f)


def build_channel_map(config: dict) -> tuple[dict, dict]:
	"""
	Returns two dicts built from the movements in the JSON config:
		midi_note_to_pb_channel : { midi_note -> program_blue_channel }
		pb_channel_to_midi_note : { program_blue_channel -> midi_note }
	"""
	midi_to_pb = {}
	pb_to_midi = {}
	for m in config.get('movements', []):
		note    = m.get('midi_note')
		channel = m.get('program_blue_channel')
		if note is not None and channel is not None:
			midi_to_pb[note]    = channel
			pb_to_midi[channel] = note
	return midi_to_pb, pb_to_midi


# ─── File discovery ────────────────────────────────────────────────────────────

def find_pair(file_path: str, extensions: tuple) -> Optional[str]:
	"""Given a file, look for a sibling file with the same stem but different extension."""
	stem = os.path.splitext(file_path)[0]
	for ext in extensions:
		candidate = stem + ext
		if os.path.exists(candidate):
			return candidate
	return None


# ─── Audio transcoding ────────────────────────────────────────────────────────

def transcode_to_mp3(audio_path: str) -> tuple[bytes, float]:
	"""Transcode any supported audio file to MP3 using ffmpeg.
	Returns (mp3_bytes, duration_seconds)."""
	with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
		tmp_path = tmp.name
	try:
		subprocess.check_call(
			['ffmpeg', '-y', '-i', audio_path,
			 '-codec:a', 'libmp3lame', '-b:a', '320k',  # CBR 320k — matches what ProgramBlue itself embeds
			 tmp_path],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		)
		duration = float(subprocess.check_output(
			['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
			 '-of', 'default=noprint_wrappers=1:nokey=1', tmp_path]
		).strip())
		with open(tmp_path, 'rb') as f:
			return f.read(), duration
	finally:
		if os.path.exists(tmp_path):
			os.unlink(tmp_path)


def extract_audio_from_shw(shw_path: str) -> tuple[bytes, int]:
	"""
	Read a v5 .shw file and return (raw_audio_bytes, audio_size).
	Raises ValueError if the file is not a recognised v5 format.
	"""
	with open(shw_path, 'rb') as f:
		data = f.read()
	m = re.match(rb'^(\d+)<dsfa>', data)
	if not m:
		raise ValueError(f"Not a v5 .shw file (no <dsfa> header): {shw_path}")
	audio_size   = int(m.group(1))
	audio_offset = m.end()
	return data[audio_offset:audio_offset + audio_size], audio_size


def write_wav_from_shw(shw_path: str, out_wav: str) -> None:
	"""Extract embedded audio from a .shw and write it as a .wav via ffmpeg."""
	audio_bytes, _ = extract_audio_from_shw(shw_path)

	with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
		tmp.write(audio_bytes)
		tmp_path = tmp.name

	try:
		subprocess.check_call(
			['ffmpeg', '-y', '-i', tmp_path, out_wav],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		)
	finally:
		os.unlink(tmp_path)
	print(f"Converter: audio written to '{out_wav}'")


# ─── v5 .shw encoding ─────────────────────────────────────────────────────────

def _encode_v5_metadata(plain: bytes) -> bytes:
	"""Additively encode plaintext metadata for the v5 .shw format."""
	return bytes((b + CIPHER_KEY[i % len(CIPHER_KEY)]) & 0xFF for i, b in enumerate(plain))


# v5 .shw preamble — 20 bytes that precede the frame table in the decoded metadata.
# Copied from a known-good .shw file; identical across all v5.01 files examined.
SHW_PREAMBLE = bytes.fromhex("ddda0c142316221f121f2423141124110eddda00")

# Every frame row carries two constant bytes at positions 255-256 (record
# separators, matching the DD DA separators seen throughout the trailer).
SHW_ROW_PAD = (0xDD, 0xDA)

# The last row of the frame table carries 0x0C at position 257 as an
# end-of-table marker. Without it ProgramBlue reads past the table into the
# trailer and crashes with an access violation.
SHW_END_MARKER = 0x0C

# Version tag appended RAW (unencoded) at the very end of the file
SHW_VERSION_TAG = b"<v5.01>"

# v5 .shw trailer — ~7.8KB block that follows the frame table inside the encoded
# metadata. Contains per-show ProgramBlue settings (channel names, UI state, etc.)
# in an undocumented format. ProgramBlue's Delphi parser does StrToInt on values
# from this block and rejects files without it ("is not a valid integer value").
# This copy was extracted from a known-good v5.01 file and is used as a template.
import base64 as _base64
SHW_TRAILER = _base64.b64decode(
	"/xQjFiIfEh8kIxQRJBEO3doMFCMWIh8SHyQjEyQdDt3aAQ0bNUI9OUQuHT9FRDgNAEg2NgAAAADd2gINGzVCPTlELhI/NEkNAEg2"
	"NgAAAADd2gMNEzgxPj41PC4DDQBINjYAAAAA3doEDRM4MT4+NTwuBA0ASDY2AAAAAN3aBQ0TODE+PjU8LgUNAEg2NgAAAADd2gYN"
	"EzgxPj41PC4GDQBINjYAAAAA3doHDRM4MT4+NTwuBw0ASDY2AAAAAN3aCA0TODE+PjU8LggNAEg2NgAAAADd2gkNEzgxPj41PC4J"
	"DQBINjYAAAAA3doBAA0TODE+PjU8LgEADQBINjYAAAAA3doBAQ0TODE+PjU8LgEBDQBINjYAAAAA3doBAg0TODE+PjU8LgECDQBI"
	"NjYAAAAA3doBAw0TODE+PjU8LgEDDQBINjYAAAAA3doBBA0TODE+PjU8LgEEDQBINjYAAAAA3doBBQ0TODE+PjU8LgEFDQBINjYA"
	"AAAA3doBBg0TODE+PjU8LgEGDQBINjYAAAAA3doBBw0TODE+PjU8LgEHDQBINjYAAAAA3doBCA0TODE+PjU8LgEIDQBINjYAAAAA"
	"3doBCQ0TODE+PjU8LgEJDQBINjYAAAAA3doCAA0TODE+PjU8LgIADQBINjYAAAAA3doCAQ0TODE+PjU8LgIBDQBINjYAAAAA3doC"
	"Ag0TODE+PjU8LgICDQBINjYAAAAA3doCAw0TODE+PjU8LgIDDQBINjYAAAAA3doCBA0TODE+PjU8LgIEDQBINjYAAAAA3doCBQ0T"
	"ODE+PjU8LgIFDQBINjYAAAAA3doCBg0TODE+PjU8LgIGDQBINjYAAAAA3doCBw0TODE+PjU8LgIHDQBINjYAAAAA3doCCA0TODE+"
	"PjU8LgIIDQBINjYAAAAA3doCCQ0TODE+PjU8LgIJDQBINjYAAAAA3doDAA0TODE+PjU8LgMADQBINjYAAAAA3doDAQ0TODE+PjU8"
	"LgMBDQBINjYAAAAA3doDAg0TODE+PjU8LgMCDQBINjYAAAAA3doDAw0TODE+PjU8LgMDDQBINjYAAAAA3doDBA0TODE+PjU8LgME"
	"DQBINjYAAAAA3doDBQ0TODE+PjU8LgMFDQBINjYAAAAA3doDBg0TODE+PjU8LgMGDQBINjYAAAAA3doDBw0TODE+PjU8LgMHDQBI"
	"NjYAAAAA3doDCA0TODE+PjU8LgMIDQBINjYAAAAA3doDCQ0TODE+PjU8LgMJDQBINjYAAAAA3doEAA0TODE+PjU8LgQADQBINjYA"
	"AAAA3doEAQ0TODE+PjU8LgQBDQBINjYAAAAA3doEAg0TODE+PjU8LgQCDQBINjYAAAAA3doEAw0TODE+PjU8LgQDDQBINjYAAAAA"
	"3doEBA0TODE+PjU8LgQEDQBINjYAAAAA3doEBQ0TODE+PjU8LgQFDQBINjYAAAAA3doEBg0TODE+PjU8LgQGDQBINjYAAAAA3doE"
	"Bw0TODE+PjU8LgQHDQBINjYAAAAA3doECA0TODE+PjU8LgQIDQBINjYAAAAA3doECQ0TODE+PjU8LgQJDQBINjYAAAAA3doFAA0T"
	"ODE+PjU8LgUADQBINjYAAAAA3doFAQ0TODE+PjU8LgUBDQBINjYAAAAA3doFAg0TODE+PjU8LgUCDQBINjYAAAAA3doFAw0TODE+"
	"PjU8LgUDDQBINjYAAAAA3doFBA0TODE+PjU8LgUEDQBINjYAAAAA3doFBQ0TODE+PjU8LgUFDQBINjYAAAAA3doFBg0TODE+PjU8"
	"LgUGDQBINjYAAAAA3doFBw0TODE+PjU8LgUHDQBINjYAAAAA3doFCA0TODE+PjU8LgUIDQBINjYAAAAA3doFCQ0TODE+PjU8LgUJ"
	"DQBINjYAAAAA3doGAA0TODE+PjU8LgYADQBINjYAAAAA3doGAQ0TODE+PjU8LgYBDQBINjYAAAAA3doGAg0TODE+PjU8LgYCDQBI"
	"NjYAAAAA3doGAw0TODE+PjU8LgYDDQBINjYAAAAA3doGBA0TODE+PjU8LgYEDQBINjYAAAAA3doGBQ0TODE+PjU8LgYFDQBINjYA"
	"AAAA3doGBg0TODE+PjU8LgYGDQBINjYAAAAA3doGBw0TODE+PjU8LgYHDQBINjYAAAAA3doGCA0TODE+PjU8LgYIDQBINjYAAAAA"
	"3doGCQ0TODE+PjU8LgYJDQBINjYAAAAA3doHAA0TODE+PjU8LgcADQBINjYAAAAA3doHAQ0TODE+PjU8LgcBDQBINjYAAAAA3doH"
	"Ag0TODE+PjU8LgcCDQBINjYAAAAA3doHAw0TODE+PjU8LgcDDQBINjYAAAAA3doHBA0TODE+PjU8LgcEDQBINjYAAAAA3doHBQ0T"
	"ODE+PjU8LgcFDQBINjYAAAAA3doHBg0TODE+PjU8LgcGDQBINjYAAAAA3doHBw0TODE+PjU8LgcHDQBINjYAAAAA3doHCA0TODE+"
	"PjU8LgcIDQBINjYAAAAA3doHCQ0TODE+PjU8LgcJDQBINjYAAAAA3doIAA0TODE+PjU8LggADQBINjYAAAAA3doIAQ0TODE+PjU8"
	"LggBDQBINjYAAAAA3doIAg0TODE+PjU8LggCDQBINjYAAAAA3doIAw0TODE+PjU8LggDDQBINjYAAAAA3doIBA0TODE+PjU8LggE"
	"DQBINjYAAAAA3doIBQ0TODE+PjU8LggFDQBINjYAAAAA3doIBg0TODE+PjU8LggGDQBINjYAAAAA3doIBw0TODE+PjU8LggHDQBI"
	"NjYAAAAA3doICA0TODE+PjU8LggIDQBINjYAAAAA3doICQ0TODE+PjU8LggJDQBINjYAAAAA3doJAA0TODE+PjU8LgkADQBINjYA"
	"AAAA3doJAQ0TODE+PjU8LgkBDQBINjYAAAAA3doJAg0TODE+PjU8LgkCDQBINjYAAAAA3doJAw0TODE+PjU8LgkDDQBINjYAAAAA"
	"3doJBA0TODE+PjU8LgkEDQBINjYAAAAA3doJBQ0TODE+PjU8LgkFDQBINjYAAAAA3doJBg0TODE+PjU8LgkGDQBINjYAAAAA3doJ"
	"Bw0TODE+PjU8LgkHDQBINjYAAAAA3doJCA0TODE+PjU8LgkIDQBINjYAAAAA3doJCQ0TODE+PjU8LgkJDQBINjYAAAAA3doBAAAN"
	"EzgxPj41PC4BAAANAEg2NgAAAADd2gEAAQ0TODE+PjU8LgEAAQ0ASDY2AAAAAN3aAQACDRM4MT4+NTwuAQACDQBINjYAAAAA3doB"
	"AAMNEzgxPj41PC4BAAMNAEg2NgAAAADd2gEABA0TODE+PjU8LgEABA0ASDY2AAAAAN3aAQAFDRM4MT4+NTwuAQAFDQBINjYAAAAA"
	"3doBAAYNEzgxPj41PC4BAAYNAEg2NgAAAADd2gEABw0TODE+PjU8LgEABw0ASDY2AAAAAN3aAQAIDRM4MT4+NTwuAQAIDQBINjYA"
	"AAAA3doBAAkNEzgxPj41PC4BAAkNAEg2NgAAAADd2gEBAA0TODE+PjU8LgEBAA0ASDY2AAAAAN3aAQEBDRM4MT4+NTwuAQEBDQBI"
	"NjYAAAAA3doBAQINEzgxPj41PC4BAQINAEg2NgAAAADd2gEBAw0TODE+PjU8LgEBAw0ASDY2AAAAAN3aAQEEDRM4MT4+NTwuAQEE"
	"DQBINjYAAAAA3doBAQUNEzgxPj41PC4BAQUNAEg2NgAAAADd2gEBBg0TODE+PjU8LgEBBg0ASDY2AAAAAN3aAQEHDRM4MT4+NTwu"
	"AQEHDQBINjYAAAAA3doBAQgNEzgxPj41PC4BAQgNAEg2NgAAAADd2gEBCQ0TODE+PjU8LgEBCQ0ASDY2AAAAAN3aAQIADRM4MT4+"
	"NTwuAQIADQBINjYAAAAA3doBAgENEzgxPj41PC4BAgENAEg2NgAAAADd2gECAg0TODE+PjU8LgECAg0ASDY2AAAAAN3aAQIDDRM4"
	"MT4+NTwuAQIDDQBINjYAAAAA3doBAgQNEzgxPj41PC4BAgQNAEg2NgAAAADd2gECBQ0TODE+PjU8LgECBQ0ASDY2AAAAAN3aAQIG"
	"DRM4MT4+NTwuAQIGDQBINjYAAAAA3doBAgcNEzgxPj41PC4BAgcNAEg2NgAAAADd2gECCA0TODE+PjU8LgECCA0ASDY2AAAAAN3a"
	"AQIJDRM4MT4+NTwuAQIJDQBINjYAAAAA3doBAwANEzgxPj41PC4BAwANAEg2NgAAAADd2gEDAQ0TODE+PjU8LgEDAQ0ASDY2AAAA"
	"AN3aAQMCDRM4MT4+NTwuAQMCDQBINjYAAAAA3doBAwMNEzgxPj41PC4BAwMNAEg2NgAAAADd2gEDBA0TODE+PjU8LgEDBA0ASDY2"
	"AAAAAN3aAQMFDRM4MT4+NTwuAQMFDQBINjYAAAAA3doBAwYNEzgxPj41PC4BAwYNAEg2NgAAAADd2gEDBw0TODE+PjU8LgEDBw0A"
	"SDY2AAAAAN3aAQMIDRM4MT4+NTwuAQMIDQBINjYAAAAA3doBAwkNEzgxPj41PC4BAwkNAEg2NgAAAADd2gEEAA0TODE+PjU8LgEE"
	"AA0ASDY2AAAAAN3aAQQBDRM4MT4+NTwuAQQBDQBINjYAAAAA3doBBAINEzgxPj41PC4BBAINAEg2NgAAAADd2gEEAw0TODE+PjU8"
	"LgEEAw0ASDY2AAAAAN3aAQQEDRM4MT4+NTwuAQQEDQBINjYAAAAA3doBBAUNEzgxPj41PC4BBAUNAEg2NgAAAADd2gEEBg0TODE+"
	"PjU8LgEEBg0ASDY2AAAAAN3aAQQHDRM4MT4+NTwuAQQHDQBINjYAAAAA3doBBAgNEzgxPj41PC4BBAgNAEg2NgAAAADd2gEECQ0T"
	"ODE+PjU8LgEECQ0ASDY2AAAAAN3aAQUADRM4MT4+NTwuAQUADQBINjYAAAAA3doBBQENEzgxPj41PC4BBQENAEg2NgAAAADd2gEF"
	"Ag0TODE+PjU8LgEFAg0ASDY2AAAAAN3aAQUDDRM4MT4+NTwuAQUDDQBINjYAAAAA3doBBQQNEzgxPj41PC4BBQQNAEg2NgAAAADd"
	"2gEFBQ0TODE+PjU8LgEFBQ0ASDY2AAAAAN3aAQUGDRM4MT4+NTwuAQUGDQBINjYAAAAA3doBBQcNEzgxPj41PC4BBQcNAEg2NgAA"
	"AADd2gEFCA0TODE+PjU8LgEFCA0ASDY2AAAAAN3aAQUJDRM4MT4+NTwuAQUJDQBINjYAAAAA3doBBgANEzgxPj41PC4BBgANAEg2"
	"NgAAAADd2gEGAQ0TODE+PjU8LgEGAQ0ASDY2AAAAAN3aAQYCDRM4MT4+NTwuAQYCDQBINjYAAAAA3doBBgMNEzgxPj41PC4BBgMN"
	"AEg2NgAAAADd2gEGBA0TODE+PjU8LgEGBA0ASDY2AAAAAN3aAQYFDRM4MT4+NTwuAQYFDQBINjYAAAAA3doBBgYNEzgxPj41PC4B"
	"BgYNAEg2NgAAAADd2gEGBw0TODE+PjU8LgEGBw0ASDY2AAAAAN3aAQYIDRM4MT4+NTwuAQYIDQBINjYAAAAA3doBBgkNEzgxPj41"
	"PC4BBgkNAEg2NgAAAADd2gEHAA0TODE+PjU8LgEHAA0ASDY2AAAAAN3aAQcBDRM4MT4+NTwuAQcBDQBINjYAAAAA3doBBwINEzgx"
	"Pj41PC4BBwINAEg2NgAAAADd2gEHAw0TODE+PjU8LgEHAw0ASDY2AAAAAN3aAQcEDRM4MT4+NTwuAQcEDQBINjYAAAAA3doBBwUN"
	"EzgxPj41PC4BBwUNAEg2NgAAAADd2gEHBg0TODE+PjU8LgEHBg0ASDY2AAAAAN3aAQcHDRM4MT4+NTwuAQcHDQBINjYAAAAA3doB"
	"BwgNEzgxPj41PC4BBwgNAEg2NgAAAADd2gEHCQ0TODE+PjU8LgEHCQ0ASDY2AAAAAN3aAQgADRM4MT4+NTwuAQgADQBINjYAAAAA"
	"3doBCAENEzgxPj41PC4BCAENAEg2NgAAAADd2gEIAg0TODE+PjU8LgEIAg0ASDY2AAAAAN3aAQgDDRM4MT4+NTwuAQgDDQBINjYA"
	"AAAA3doBCAQNEzgxPj41PC4BCAQNAEg2NgAAAADd2gEIBQ0TODE+PjU8LgEIBQ0ASDY2AAAAAN3aAQgGDRM4MT4+NTwuAQgGDQBI"
	"NjYAAAAA3doBCAcNHDk3OEQuEjFCLiNAP0QuEzw1MUINAEg2NgAAAADd2gEICA0cOTc4RC4SMUIuI0A/RC4iNTQuAQ0ASDY2AAAA"
	"AN3aAQgJDRw5NzhELhIxQi4jQD9ELhI8RTUNAEg2NgAAAADd2gEJAA0cOTc4RC4SMUIuI0A/RC4iNTQuAg0ASDY2AAAAAN3aAQkB"
	"DRw5NzhELhIxQi4jQD9ELhE9MjVCDQBINjYAAAAA3doBCQINFzU/Qjc1Lh0/RUQ4DQBINjYAAAAA3doBCQMNFzU/Qjc1LhI/NEku"
	"Ij8zOw0ASDY2AAAAAN3aAQkEDRc1P0I3NS4YNTE0LiI5NzhEDQBINjYAAAAA3doBCQUNFzU/Qjc1Lhg1MTQuHDU2RA0ASDY2AAAA"
	"AN3aAQkGDRc1P0I3NS4jREJFPQ0ASDY2AAAAAN3aAQkHDRM4MT4+NTwuAQkHDQBINjYAAAAA3doBCQgNEzgxPj41PC4BCQgNAEg2"
	"NgAAAADd2gEJCQ0TODE+PjU8LgEJCQ0ASDY2AAAAAN3aAgAADRw5NzhELiNEMTc1LiI5NzhEDQBINjYAAAAA3doCAAENHDk3OEQu"
	"I0QxNzUuIjk3OEQuEzU+RDVCDQBINjYAAAAA3doCAAINHDk3OEQuI0QxNzUuEzU+RDVCDQBINjYAAAAA3doCAAMNHDk3OEQuI0Qx"
	"NzUuHDU2RC4TNT5ENUINAEg2NgAAAADd2gIABA0cOTc4RC4jRDE3NS4cNTZEDQBINjYAAAAA3doCAAUNI0Q/PzxDDQBINjYAAAAA"
	"3doCAAYNGj84Pi4dP0VEOA0ASDY2AAAAAN3aAgAHDRo/OD4uEj80SS4iPzM7DQBINjYAAAAA3doCAAgNGj84Pi4YNTE0LiI5NzhE"
	"DQBINjYAAAAA3doCAAkNGj84Pi4YNTE0Lhw1NkQNAEg2NgAAAADd2gIBAA0aPzg+Lh05My4jRzk+Nw0ASDY2AAAAAN3aAgEBDRo/"
	"OD4uFj8/RA0ASDY2AAAAAN3aAgECDRI1MTc8NUMuIzk3Pg0ASDY2AAAAAN3aAgEDDRI1MTc8NUMuJEI5Rj88OQ0ASDY2AAAAAN3a"
	"AgEEDSAxRTwuHT9FRDgNAEg2NgAAAADd2gIBBQ0gMUU8LhI/NEkuIj8zOw0ASDY2AAAAAN3aAgEGDSAxRTwuGDUxNC4iOTc4RA0A"
	"SDY2AAAAAN3aAgEHDSAxRTwuGDUxNC4cNTZEDQBINjYAAAAA3doCAQgNIDFFPC4jREJFPQ0ASDY2AAAAAN3aAgEJDRM4MT4+NTwu"
	"AgEJDQBINjYAAAAA3doCAgANEzgxPj41PC4CAgANAEg2NgAAAADd2gICAQ0TODE+PjU8LgICAQ0ASDY2AAAAAN3aAgICDSI5Pjc/"
	"Lh0/RUQ4DQBINjYAAAAA3doCAgMNIjk+Nz8uEj80SS4iPzM7DQBINjYAAAAA3doCAgQNEzgxPj41PC4CAgQNAEg2NgAAAADd2gIC"
	"BQ0TODE+PjU8LgICBQ0ASDY2AAAAAN3aAgIGDSI5Pjc/Lhw1NkQuEUI9DQBINjYAAAAA3doCAgcNIjk+Nz8uIjk3OEQuEUI9DQBI"
	"NjYAAAAA3doCAggNEzgxPj41PC4CAggNAEg2NgAAAADd2gICCQ0TODE+PjU8LgICCQ0ASDY2AAAAAN3aAgMADRM4MT4+NTwuAgMA"
	"DQBINjYAAAAA3doCAwENEzgxPj41PC4CAwENAEg2NgAAAADd2gIDAg0TODE+PjU8LgIDAg0ASDY2AAAAAN3aAgMDDRM4MT4+NTwu"
	"AgMDDQBINjYAAAAA3doCAwQNEzgxPj41PC4CAwQNAEg2NgAAAADd2gIDBQ0TODE+PjU8LgIDBQ0ASDY2AAAAAN3aAgMGDRM4MT4+"
	"NTwuAgMGDQBINjYAAAAA3doCAwcNEzgxPj41PC4CAwcNAEg2NgAAAADd2gIDCA0TODE+PjU8LgIDCA0ASDY2AAAAAN3aAgMJDRM4"
	"MT4+NTwuAgMJDQBINjYAAAAA3doCBAANEzgxPj41PC4CBAANAEg2NgAAAADd2gIEAQ0TODE+PjU8LgIEAQ0ASDY2AAAAAN3aAgQC"
	"DRM4MT4+NTwuAgQCDQBINjYAAAAA3doCBAMNEzgxPj41PC4CBAMNAEg2NgAAAADd2gIEBA0TODE+PjU8LgIEBA0ASDY2AAAAAN3a"
	"AgQFDRM4MT4+NTwuAgQFDQBINjYAAAAA3doCBAYNEzgxPj41PC4CBAYNAEg2NgAAAADd2gIEBw0TODE+PjU8LgIEBw0ASDY2AAAA"
	"AN3aAgQIDRM4MT4+NTwuAgQIDQBINjYAAAAA3doCBAkNEzgxPj41PC4CBAkNAEg2NgAAAADd2gIFAA0TODE+PjU8LgIFAA0ASDY2"
	"AAAAAN3aAgUBDRM4MT4+NTwuAgUBDQBINjYAAAAA3doCBQINEzgxPj41PC4CBQINAEg2NgAAAADd2gIFAw0TODE+PjU8LgIFAw0A"
	"SDY2AAAAAN3aAgUEDRM4MT4+NTwuAgUEDQBINjYAAAAA3doCBQUNEzgxPj41PC4CBQUNAEg2NgAAAADd2gIFBg0TODE+PjU8LgIF"
	"Bg0ASDY2AAAAAN3aAgUHDRYxREoNAAkE/QAHBf0ABwb9AAkD/QAIBv0ACAf9AAgD/QAIBP0ACAX9AAgJ/QAJAf0ABwP9AAcH/QAJ"
	"AP0ACQL9AAcE/QAABf3d2gIFCA0iPzw2NQ0AAwL9AAMA/QADAf0AAQD9AAAJ/QAACP0AAAf9AAIA/QACAf0AAgL9AAID/QACBP0A"
	"Agf9AAII/QACBf0AAgn9AAEI/QABB/0AAgb9AAEJ/d3aAgUJDRUxQjwNAAMF/QADA/0AAwT9AAUD/QADBv0ABQL93doCBgANEjUx"
	"MzguEjUxQg0BBQH9AQMJ/QEEAf0BBAD9AQMG/QEEAv0BBAP9AQQE/QEDB/0BBAX9AQUC/QEEB/0BBAj9AQMI/QEFAP0BBAb9AAAC"
	"/QEECf3d2gIGAQ0dOURKOQ0BBwT9AQYB/QEHA/0BBgn9AQcA/QEGBP0BBgX9AQYG/QEFCf0BBwL9AQYC/QEGAP0BBgf9AQcB/QEF"
	"BP0BBQb9AQYD/QEFBf0BBgj9AAAB/d3aAgYCDRI5PDxJLhI/Mg0CAQX9AgED/QIBBP0CAAH9AgAC/QIDAv0BCQb9AQcF/QEHBv0C"
	"AAP9AgAE/QIABf0CAAb9AgAH/QEJCf0BCQj9AgAI/QIACf0CAAD9AgEB/QAABP0CAQD93doCBgMNHD8/PjVJLhI5QjQNAQkF/QAI"
	"AP0BBQf9AQkD/QEFA/0BCQT9AAAD/QEFCP3d2gIGBA0SOTw8SS4jRDE3NS4cOTc4REMNAgMA/QICCP0CAgn9AgMB/QEDA/0BAwH9"
	"AQMC/QIDAv3d2gIGBQ0TNT5ENUIuI0QxNzUuHDk3OERDDQICBf0CAgP9AgIE/QICBv0BAgL9AQID/QECAf0BAgb9AQIH/QECBf0B"
	"AgT9AQAI/QEACf0BAQH9AQEC/QEBAP3d2gIGBg0iPzw2NS4jRDE3NS4cOTc4REMNAQMF/QEDBP0CAgD9AgEI/QIBCf0CAgH93doC"
	"BgcNE0VCRDE5PkMNAAEC/QABAf0AAQD9AAAJ/QABBP0AAQP93doCBggNIEI/QEMNAAgB/QEDAP0ACAL9AAUE/QAFBf0BAgj9AAcI"
	"/QAHCf0BAgn93doCBgkNFjFESi4fQjcxPi4cOTc4REMNAQAF/QEABP0BAAP9AQAG/QEAB/0BAAD9AAkI/QEAAf0ACQn93doCBwAN"
	"FD8/Ow0ACQX9AAkG/QAEBP0ABAX9AAQB/QADB/0AAwj9AAQI/QAECf0AAwn9AAUB/QAEAv0ABAb9AAUA/QAEAP0ABAf9AAQD/QAA"
	"Bv0CAgf93doM/xQjFiIfEh8kIxMkHQ7d2gwUIxYiHxIfJCMgIh8aFRMkIxUkJBkeFyMO3dogQj86NTNEHjE9NQ0kNUNEAt3aFiAj"
	"DQQA3doRRTQ5PxVIRDU+Qzk/Pg09QAPd2hFFRDg/Qh4xPTUNET40QjVH3doiNUY5Qzk/Pg0C3doiNTc5Q0RCMUQ5Pz4N3doSP0U+"
	"MzVCDd3aIEI/N0IxPSQ5PTUNAgDd2hFFNDk/QDUxOw3d2hFFNDk/MzgxPg0A3doTQjUxRDU0DQAH/wAH/wIAAgYuAQkKBAMKAgjd"
	"2iY5NDU/HzY2QzVEDQDd2gz/FCMWIh8SHyQjICIfGhUTJCMVJCQZHhcjDt3a8wIDAgAIAAA="
)


def events_to_shw(events: list, audio_mp3: bytes, fps: int = SHW_FPS, audio_duration_s: float = 0.0) -> bytes:
	"""
	Convert a list of [timestamp_ms, pb_channel, value] events and MP3 audio
	bytes into a v5 .shw binary.

	v5 structure:
		{audio_size}<dsfa>{audio_bytes}{xor_encoded_frame_table}<
	"""
	# Frame count: at least long enough for the audio (+1 for the end marker
	# row), and never shorter than the last event.
	import math
	audio_frames = math.ceil(audio_duration_s * fps) + 1 if audio_duration_s > 0 else 0
	event_frames = int(max(e[0] for e in events) * fps / 1000) + 1 if events else 0
	frame_count  = max(audio_frames, event_frames)

	# Build frame table buffer: preamble + frame rows
	table = bytearray(SHW_PREAMBLE) + bytearray(frame_count * FRAME_STRIDE)

	# Replay events into per-frame state
	# Build a state array: state[frame][channel_1_indexed]
	state = [[0] * (NUM_SHW_CHANNELS + 1) for _ in range(frame_count)]

	# Forward-fill: convert edge events into full frame states
	current = [0] * (NUM_SHW_CHANNELS + 1)
	event_by_frame: dict[int, list] = {}
	for ts_ms, channel, value in sorted(events, key=lambda e: e[0]):
		frame = min(int(ts_ms * fps / 1000), frame_count - 1)
		event_by_frame.setdefault(frame, []).append((channel, value))

	for frame in range(frame_count):
		if frame in event_by_frame:
			for channel, value in event_by_frame[frame]:
				if 1 <= channel <= NUM_SHW_CHANNELS:
					current[channel] = value
		state[frame] = list(current)

	# Write rows into the table buffer
	for frame in range(frame_count):
		row_start = FRAME_BASE + frame * FRAME_STRIDE
		for channel in range(1, NUM_SHW_CHANNELS + 1):
			val = state[frame][channel]
			if channel == 1:
				pos = 257
			else:
				pos = channel - 2  # channels 2-256 → positions 0-254
			table[row_start + pos] = 0x01 if val else 0x00
		# Positions 255-256 hold constant record separator bytes
		table[row_start + 255] = SHW_ROW_PAD[0]
		table[row_start + 256] = SHW_ROW_PAD[1]

	# End-of-table marker in the last row (overwrites any channel 1 value there)
	if frame_count > 0:
		table[FRAME_BASE + (frame_count - 1) * FRAME_STRIDE + 257] = SHW_END_MARKER

	# Assemble: preamble + frame rows + trailer (all XOR-encoded), then RAW version tag.
	# Note: good files have NO '<' terminator between the frame table and trailer —
	# the raw "<v5.01>" at the end of the file is the actual end marker.
	raw_meta    = bytes(table) + SHW_TRAILER
	encoded     = _encode_v5_metadata(raw_meta)

	header      = f"{len(audio_mp3)}<dsfa>".encode()
	return header + audio_mp3 + encoded + SHW_VERSION_TAG


# ─── MIDI parsing / writing ────────────────────────────────────────────────────

def parse_midi_events(midi_path: str) -> list:
	"""
	Parse a MIDI file into [timestamp_ms, midi_note, value] events.
	Mirrors midi.parse_file() from midi.py.
	"""
	events: list = []
	try:
		midi_file       = mido.MidiFile(midi_path)
		current_time_ms = 0.0
		for message in midi_file:
			current_time_ms += message.time * 1000
			if message.type == 'note_on':
				value = 1 if message.velocity >= 90 else 0
				events.append([current_time_ms, message.note, value])
			elif message.type == 'note_off':
				events.append([current_time_ms, message.note, 0])
	except Exception as e:
		print(f"Converter: failed to parse MIDI '{midi_path}': {e}")
	return events


def write_midi(events: list, out_path: str, pb_to_midi: dict) -> None:
	"""
	Write [timestamp_ms, pb_channel, value] events to a MIDI file.

	Each program_blue_channel gets its own MIDI channel (1-16, wrapping).
	The midi_note for each channel comes from pb_to_midi.
	Uses 120 BPM and MIDI_PPQ ticks per quarter note.
	"""
	midi_file = mido.MidiFile(type=1, ticks_per_beat=MIDI_PPQ)

	# Tempo track
	tempo_track = mido.MidiTrack()
	tempo_track.append(mido.MetaMessage('set_tempo', tempo=MIDI_TEMPO, time=0))
	tempo_track.append(mido.MetaMessage('end_of_track', time=0))
	midi_file.tracks.append(tempo_track)

	# Build one track per pb_channel that appears in the events
	pb_channels_seen = sorted(set(e[1] for e in events))

	# Assign MIDI channels (1-indexed, wrapping 1-16)
	midi_channel_for = {
		pb_ch: ((pb_ch - 1) % 16)  # mido uses 0-indexed channels internally
		for pb_ch in pb_channels_seen
	}

	def ms_to_ticks(ms: float) -> int:
		# 120 BPM: one beat = 500 ms, so ticks_per_ms = MIDI_PPQ / 500
		return round(ms * MIDI_PPQ / 500.0)

	for pb_ch in pb_channels_seen:
		note       = pb_to_midi.get(pb_ch, 60)  # fallback to middle C
		midi_ch    = midi_channel_for[pb_ch]
		ch_events  = sorted(
			[(e[0], e[2]) for e in events if e[1] == pb_ch],
			key=lambda x: x[0]
		)

		track = mido.MidiTrack()
		track.append(mido.MetaMessage(
			'track_name',
			name=f"PB ch{pb_ch}",
			time=0,
		))

		# Set channel volume to max
		track.append(mido.Message(
			'control_change',
			channel=midi_ch,
			control=7,
			value=127,
			time=0,
		))

		prev_ticks = 0
		for ts_ms, value in ch_events:
			abs_ticks = ms_to_ticks(ts_ms)
			delta     = abs_ticks - prev_ticks
			prev_ticks = abs_ticks

			if value == 1:
				track.append(mido.Message(
					'note_on',
					channel=midi_ch,
					note=note,
					velocity=127,
					time=delta,
				))
			else:
				track.append(mido.Message(
					'note_off',
					channel=midi_ch,
					note=note,
					velocity=0,
					time=delta,
				))

		track.append(mido.MetaMessage('end_of_track', time=0))
		midi_file.tracks.append(track)

	midi_file.save(out_path)
	print(f"Converter: MIDI written to '{out_path}'")


# ─── Conversion logic ──────────────────────────────────────────────────────────

def midi_and_audio_to_shw(midi_path: str, audio_path: str, out_shw: str, midi_to_pb: dict) -> None:
	"""Convert a MIDI + audio pair into a v5 .shw file."""
	print(f"Converter: reading MIDI '{midi_path}'")
	midi_events = parse_midi_events(midi_path)

	# Map midi_note events to pb_channel events
	shw_events = []
	for ts_ms, note, value in midi_events:
		pb_channel = midi_to_pb.get(note)
		if pb_channel is not None:
			shw_events.append([ts_ms, pb_channel, value])
		else:
			print(f"Converter: warning — MIDI note {note} at {ts_ms:.0f}ms has no mapping, skipping")

	print(f"Converter: {len(midi_events)} MIDI events → {len(shw_events)} channel events")

	print(f"Converter: transcoding audio '{audio_path}' to MP3...")
	audio_mp3, duration_s = transcode_to_mp3(audio_path)
	print(f"Converter: audio is {len(audio_mp3)} bytes, {duration_s:.2f}s")

	shw_data = events_to_shw(shw_events, audio_mp3, audio_duration_s=duration_s)

	with open(out_shw, 'wb') as f:
		f.write(shw_data)
	print(f"Converter: .shw written to '{out_shw}'")


def parse_shw_events(shw_path: str, fps: int = SHW_FPS) -> list:
	"""
	Self-contained v5 .shw frame table parser.
	Returns a list of [timestamp_ms, pb_channel, value] events.
	Mirrors parse_file() from program_blue.py.
	"""
	def frame_to_ms(frame: int) -> int:
		return round(frame * 1000.0 / fps)

	def decode_metadata(meta: bytes) -> bytes:
		return bytes((b - CIPHER_KEY[i % len(CIPHER_KEY)]) & 0xFF for i, b in enumerate(meta))

	def pos_to_channel(pos: int):
		if pos == 257:
			return 1
		if 0 <= pos <= 254:
			return pos + 2
		return None

	with open(shw_path, 'rb') as f:
		data = f.read()

	m = re.match(rb'^(\d+)<dsfa>', data)
	if not m:
		raise ValueError(f"Not a v5 .shw file: {shw_path}")

	audio_size   = int(m.group(1))
	audio_offset = m.end()
	meta         = data[audio_offset + audio_size:]

	# Strip trailing version tag e.g. "v5.1>"
	ver = re.search(rb'v\d+\.\d+>$', meta)
	if ver:
		meta = meta[:ver.start()]

	decoded     = decode_metadata(meta)
	table_end   = decoded.find(b'<', 512)
	if table_end == -1:
		raise ValueError("Could not find v5 frame table terminator")

	frame_count = (table_end - FRAME_BASE) // FRAME_STRIDE
	if frame_count <= 0:
		raise ValueError("Could not detect v5 frame table")

	# Trim trailing blank rows
	while frame_count > 0:
		row_start = FRAME_BASE + (frame_count - 1) * FRAME_STRIDE
		row = decoded[row_start:row_start + FRAME_STRIDE]
		channel_bytes = row[:255] + row[257:258]
		if any(channel_bytes):
			break
		frame_count -= 1

	events = []
	prev   = [0] * NUM_SHW_CHANNELS

	for frame in range(frame_count):
		row_start = FRAME_BASE + frame * FRAME_STRIDE
		row       = decoded[row_start:row_start + FRAME_STRIDE]
		if len(row) < FRAME_STRIDE:
			break

		current = [0] * NUM_SHW_CHANNELS
		for pos, b in enumerate(row):
			# Skip the end-of-table marker in the last row (not channel data)
			if frame == frame_count - 1 and pos == 257 and b == 0x0C:
				continue
			ch = pos_to_channel(pos)
			if ch is not None:
				current[ch - 1] = 1 if b else 0

		for ch_idx, value in enumerate(current):
			if value != prev[ch_idx]:
				events.append([frame_to_ms(frame), ch_idx + 1, value])

		prev = current

	print(f"Converter: parsed {len(events)} channel events from '{shw_path}'")
	return events


def shw_to_midi_and_audio(shw_path: str, out_midi: str, out_wav: str, pb_to_midi: dict) -> None:
	"""Convert a v5 .shw file into a MIDI file and a .wav audio file."""
	print(f"Converter: parsing '{shw_path}'")
	shw_events = parse_shw_events(shw_path)
	write_midi(shw_events, out_midi, pb_to_midi)
	write_wav_from_shw(shw_path, out_wav)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
	if len(sys.argv) != 3:
		print("Usage: python3 programBlue_midi_converter.py <file> <config.json>")
		sys.exit(1)

	input_file  = sys.argv[1]
	config_path = sys.argv[2]

	if not os.path.exists(input_file):
		print(f"Error: input file not found: '{input_file}'", file=sys.stderr)
		sys.exit(1)
	if not os.path.exists(config_path):
		print(f"Error: config not found: '{config_path}'", file=sys.stderr)
		sys.exit(1)

	config              = load_config(config_path)
	midi_to_pb, pb_to_midi = build_channel_map(config)

	ext  = os.path.splitext(input_file)[1].lower()
	stem = os.path.splitext(os.path.abspath(input_file))[0]

	if ext in MIDI_EXTENSIONS:
		audio_path = find_pair(input_file, AUDIO_EXTENSIONS)
		if not audio_path:
			print(
				f"Error: no matching audio file found for '{input_file}' "
				f"(looked for {', '.join(AUDIO_EXTENSIONS)})",
				file=sys.stderr,
			)
			sys.exit(1)
		midi_and_audio_to_shw(input_file, audio_path, stem + '.shw', midi_to_pb)

	elif ext in AUDIO_EXTENSIONS:
		midi_path = find_pair(input_file, MIDI_EXTENSIONS)
		if not midi_path:
			print(
				f"Error: no matching MIDI file found for '{input_file}' "
				f"(looked for {', '.join(MIDI_EXTENSIONS)})",
				file=sys.stderr,
			)
			sys.exit(1)
		midi_and_audio_to_shw(midi_path, input_file, stem + '.shw', midi_to_pb)

	elif ext == '.shw':
		shw_to_midi_and_audio(input_file, stem + '.mid', stem + '.wav', pb_to_midi)

	else:
		print(
			f"Error: unrecognised file type '{ext}'. "
			f"Expected {MIDI_EXTENSIONS + AUDIO_EXTENSIONS + ('.shw',)}",
			file=sys.stderr,
		)
		sys.exit(1)


if __name__ == "__main__":
	main()
