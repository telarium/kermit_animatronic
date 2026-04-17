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
		#if value == 1:
			# Note on message with full velocity
			# msg = mido.Message('note_on', note=note, velocity=127)
		#else:
			# Note off message with lowest velocity
			# msg = mido.Message('note_off', note=note, velocity=0)
		# self.outport.send(msg)
		#print(f"Sent MIDI message: {msg}")
		print("TODO MIDI SEND")

# Example usage:
if __name__ == "__main__":
	midi = MIDI()
	# Keep the application running to receive callbacks
	input("Press Enter to exit...\n")
