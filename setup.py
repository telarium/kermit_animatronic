#!/usr/bin/env python3

import os
import sys
import subprocess
from typing import List


class Setup:
	def __init__(self) -> None:
		# List of system packages to install (from apt)
		packages: List[str] = [
			"git", "build-essential", "python3-dev", "flex", "bison", "mpv", "hostapd", "dnsmasq",
			"python3-smbus", "python3-evdev", "python3-setuptools", "python3-mido",
			"python3-flask", "python3-flask-socketio", "python3-pip",
			"python3-psutil", "python3-pydispatch", "python3-pygame", "iw",
			"iproute2"
		]

		self._run_command("sudo apt update")

		# Install system packages using subprocess and handle potential errors
		self.install_packages(packages)

		# Install Python dependencies via pip with --break-system-packages
		self.install_python_packages([
			"pvporcupine", "pvrhino", "pydub", "scipy", "openai", "elevenlabs", "piper-tts", "pywifi", "flask-talisman", "requests"
		])

		# Set up Piper TTS models
		self.setup_piper_models()

	def install_packages(self, packages: List[str]) -> None:
		try:
			# Install packages using apt
			subprocess.check_call(["sudo", "apt", "install", "-y"] + packages)
		except subprocess.CalledProcessError as e:
			print(f"Failed to install packages: {e}")
			sys.exit(1)

	def install_python_packages(self, packages: List[str]) -> None:
		subprocess.check_call(["sudo", "/usr/bin/python", "-m", "pip", "install", "--upgrade", "pip"])
		try:
			for package in packages:
				# Use pip with --break-system-packages flag to allow installation
				subprocess.check_call(
					["sudo", sys.executable, "-m", "pip", "install", "--break-system-packages", package]
				)
		except subprocess.CalledProcessError as e:
			print(f"Failed to install Python packages: {e}")
			sys.exit(1)

	def setup_piper_models(self) -> None:
		try:
			# Get the directory of the current script
			script_dir = os.path.dirname(os.path.abspath(__file__))

			# Download Ryan Low voice model into the script's directory
			subprocess.check_call([
				"wget",
				"-O", os.path.join(script_dir, "en_US-ryan-low.onnx"),
				"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/ryan/low/en_US-ryan-low.onnx?download=true"
			])
			subprocess.check_call([
				"wget",
				"-O", os.path.join(script_dir, "en_US-ryan-low.json"),
				"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/ryan/low/en_US-ryan-low.onnx.json?download=true"
			])

			print(f"Piper TTS models are available in {script_dir}.")
		except subprocess.CalledProcessError as e:
			print(f"Failed to set up Piper models: {e}")
			sys.exit(1)

	def _run_command(self, command: str) -> None:
		try:
			subprocess.check_call(command, shell=True)
		except subprocess.CalledProcessError as e:
			print(f"Command failed: {command}\nError: {e}")
			sys.exit(1)


if __name__ == "__main__":
	Setup()
