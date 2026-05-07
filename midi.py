import subprocess
from pydispatch import dispatcher
import mido
from typing import Optional, List

class MIDI:
	def __init__(self, input_port_name: Optional[str] = None, output_port_name: Optional[str] = None) -> None:
		print("TODO")

	def _find_default_port(self, port_list: List[str]) -> Optional[str]:
		return None

	def _midi_callback(self, message: mido.Message) -> None:
		# Callback function that is called when a MIDI message is received.
		# (Note: fixed the variable name 'velocity' to 'value')
		# print("Received MIDI message:", message)
		print("TODO MIDI RECEIVE")

	def send_message(self, note: int, value: int) -> None:
		if not hasattr(self, 'outport') or self.outport is None:
			return

		if value == 1:
			# Note on message with full velocity
			msg = mido.Message('note_on', note=note, velocity=127)
		else:
			# Note off message with lowest velocity
			msg = mido.Message('note_off', note=note, velocity=0)
		self.outport.send(msg)
		print(f"Sent MIDI message: {msg}")

def parse_file(self, file: str) -> List[List]:
	"""Parse a MIDI file and return a list of [time_ms, midi_note, value] events.
	value is 1 if velocity >= 90, 0 otherwise (note off or soft note on)."""
	events: List[List] = []
	try:
		midi_file = mido.MidiFile(file)
		current_time_ms: float = 0.0
		for message in midi_file:
			current_time_ms += message.time * 1000  # delta time → absolute ms
			if message.type == 'note_on':
				value = 1 if message.velocity >= 90 else 0
				events.append([current_time_ms, message.note, value])
			elif message.type == 'note_off':
				events.append([current_time_ms, message.note, 0])
	except Exception as e:
		print(f"MIDI: failed to parse '{file}': {e}")
	return events


# Example usage:
if __name__ == "__main__":
	midi = MIDI()
	# Keep the application running to receive callbacks
	input("Press Enter to exit...\n")
