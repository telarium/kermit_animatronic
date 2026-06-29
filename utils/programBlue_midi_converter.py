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

XOR_KEY = bytes.fromhex(
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

def transcode_to_mp3(audio_path: str) -> bytes:
	"""Transcode any supported audio file to MP3 bytes using ffmpeg."""
	with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
		tmp_path = tmp.name
	try:
		subprocess.check_call(
			['ffmpeg', '-y', '-i', audio_path, '-codec:a', 'libmp3lame', '-q:a', '2', tmp_path],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		)
		with open(tmp_path, 'rb') as f:
			return f.read()
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

def _encode_v5_metadata(raw: bytes) -> bytes:
	"""XOR-encode raw frame table bytes for the v5 .shw metadata block."""
	return bytes(b ^ XOR_KEY[i % len(XOR_KEY)] for i, b in enumerate(raw))


def events_to_shw(events: list, audio_mp3: bytes, fps: int = SHW_FPS) -> bytes:
	"""
	Convert a list of [timestamp_ms, pb_channel, value] events and MP3 audio
	bytes into a v5 .shw binary.

	v5 structure:
		{audio_size}<dsfa>{audio_bytes}{xor_encoded_frame_table}<
	"""
	# Determine total frame count from the last event timestamp
	if not events:
		frame_count = 0
	else:
		last_ms     = max(e[0] for e in events)
		frame_count = int(last_ms * fps / 1000) + 1

	# Build frame table: frame_count rows of FRAME_STRIDE bytes each
	# Layout per row: bytes 0-254 = channels 2-256, byte 257 = channel 1
	# (mirrors the pos_to_channel logic in program_blue.py)
	table = bytearray(FRAME_BASE + frame_count * FRAME_STRIDE)

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
			table[row_start + pos] = 0xFF if val else 0x00

	# Terminate frame table with '<'
	raw_meta    = bytes(table) + b'<'
	encoded     = _encode_v5_metadata(raw_meta)

	header      = f"{len(audio_mp3)}<dsfa>".encode()
	return header + audio_mp3 + encoded


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
	audio_mp3 = transcode_to_mp3(audio_path)
	print(f"Converter: audio is {len(audio_mp3)} bytes")

	shw_data = events_to_shw(shw_events, audio_mp3)

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
		return bytes(b ^ XOR_KEY[i % len(XOR_KEY)] for i, b in enumerate(meta))

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
