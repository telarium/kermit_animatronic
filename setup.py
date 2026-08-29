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
			"python3-flask", "python3-pip",
			"python3-psutil", "python3-pydispatch", "python3-pygame", "iw",
			"iproute2",
			# ALSA audio
			"alsa-utils", "portaudio19-dev", "ffmpeg",
		]
		self._run_command("sudo apt update")
		self.install_packages(packages)
		self.install_python_packages([
			# Flask-SocketIO and its dependencies — pinned for compatibility.
			# Do NOT install python3-flask-socketio via apt; the system version
			# is too old and causes AttributeError on _request_ctx_stack.
			"werkzeug==2.3.7",
			"flask==2.3.3",
			"flask-socketio==5.3.6",
			"python-socketio==5.10.0",
			"python-engineio==4.8.0",
			# numpy pinned to <2.0 for openwakeword compatibility
			"numpy<2.0",
			# Other pip-only packages
			"pvporcupine", "rapidfuzz", "pydub", "scipy", "openai", "elevenlabs", "piper-tts",
			"pywifi", "flask-talisman", "requests", "openwakeword", "pyudev", "anthropic", "smbus2",
			# USB — pip-only, no apt equivalents
			"pyusb",
			# STT. sherpa-onnx bundles its own onnxruntime, so nothing else is
			# needed here. webrtcvad and pyaudio were dropped with whisper.cpp:
			# the streaming transducer does its own endpointing, and both the
			# wakeword and STT capture paths use arecord rather than PortAudio.
			"sherpa-onnx",
			# USB-serial for ProgramBlue / PL2303 adapter
			"pyserial",
		])
		self.setup_piper_models()
		self.setup_sherpa_models()
		self.setup_openwakeword_models()
		self.setup_respeaker()
		self.setup_pl2303()
		self.setup_bashrc()

	def install_packages(self, packages: List[str]) -> None:
		try:
			subprocess.check_call(["sudo", "apt", "install", "-y"] + packages)
		except subprocess.CalledProcessError as e:
			print(f"Failed to install packages: {e}")
			sys.exit(1)

	def install_python_packages(self, packages: List[str]) -> None:
		# Upgrade pip for the SAME interpreter used for the installs below.
		# This previously hardcoded /usr/bin/python, which on JetPack is not
		# necessarily sys.executable — so pip could be upgraded for one
		# interpreter while packages landed in another.
		subprocess.check_call(
			["sudo", sys.executable, "-m", "pip", "install",
			 "--break-system-packages", "--upgrade", "pip"]
		)
		try:
			for package in packages:
				subprocess.check_call(
					["sudo", sys.executable, "-m", "pip", "install", "--break-system-packages", package]
				)
		except subprocess.CalledProcessError as e:
			print(f"Failed to install Python packages: {e}")
			sys.exit(1)

	def setup_piper_models(self) -> None:
		try:
			script_dir = os.path.dirname(os.path.abspath(__file__))
			subprocess.check_call([
				"wget", "-O", os.path.join(script_dir, "en_US-ryan-low.onnx"),
				"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/ryan/low/en_US-ryan-low.onnx?download=true"
			])
			subprocess.check_call([
				"wget", "-O", os.path.join(script_dir, "en_US-ryan-low.json"),
				"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/ryan/low/en_US-ryan-low.onnx.json?download=true"
			])
			print(f"Piper TTS models are available in {script_dir}.")
		except subprocess.CalledProcessError as e:
			print(f"Failed to set up Piper models: {e}")
			sys.exit(1)

	# Streaming transducer used by speech_to_text.py. The 560ms chunk export
	# is the accuracy/latency sweet spot: larger chunks are *both* more
	# accurate and cheaper (fewer encoder invocations per second), but 1120ms
	# adds noticeable dead air before Kermit responds.
	SHERPA_MODEL = "sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25"
	SHERPA_MODEL_URL = (
		"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
		f"{SHERPA_MODEL}.tar.bz2"
	)

	def setup_sherpa_models(self) -> None:
		"""Download the Nemotron streaming transducer into lib/sherpa_onnx/models/.

		This is a ~630MB download and will take a while on first run. The
		path must match SpeechToText.MODEL_DIR in speech_to_text.py.
		"""
		script_dir = os.path.dirname(os.path.abspath(__file__))
		models_dir = os.path.join(script_dir, "lib", "sherpa_onnx", "models")
		model_dir  = os.path.join(models_dir, self.SHERPA_MODEL)

		# Check for an actual model file, not just the directory — a download
		# interrupted mid-extract leaves the directory behind and would
		# otherwise be treated as a completed install.
		if os.path.exists(os.path.join(model_dir, "encoder.int8.onnx")):
			print(f"Sherpa model {self.SHERPA_MODEL} already present, skipping.")
			return

		os.makedirs(models_dir, exist_ok=True)
		archive = os.path.join(models_dir, f"{self.SHERPA_MODEL}.tar.bz2")
		print(f"Downloading sherpa-onnx model: {self.SHERPA_MODEL} (~630MB)")
		try:
			subprocess.check_call(["wget", "-O", archive, self.SHERPA_MODEL_URL])
			subprocess.check_call(["tar", "xf", archive, "-C", models_dir])
		except subprocess.CalledProcessError as e:
			print(f"Failed to set up sherpa-onnx model: {e}")
			sys.exit(1)
		finally:
			if os.path.exists(archive):
				os.remove(archive)

		print(f"Sherpa model installed to {model_dir}.")

	def setup_openwakeword_models(self) -> None:
		"""Download openWakeWord base models and install custom hey_kermit model."""
		try:
			import openwakeword
			openwakeword.utils.download_models()
			print("openWakeWord base models downloaded.")
		except Exception as e:
			print(f"Failed to download openWakeWord models: {e}")
			sys.exit(1)

		# Copy the custom wakeword model into the openwakeword resources folder.
		# Note: wakeword_detection.py loads this by absolute path from
		# lib/openwakeword/ (see kermit.json), so this copy is belt-and-braces
		# rather than required. The filename is okay_ker_mit.onnx — the old
		# hey_ker_mit.onnx name here never matched what ships in the repo, so
		# this step printed a WARNING on every run.
		script_dir = os.path.dirname(os.path.abspath(__file__))
		model_name = "okay_ker_mit.onnx"
		src = os.path.join(script_dir, "lib", "openwakeword", model_name)
		dst = f"/usr/local/lib/python3.10/dist-packages/openwakeword/resources/models/{model_name}"
		if os.path.exists(src):
			subprocess.check_call(["sudo", "cp", src, dst])
			print(f"{model_name} installed to openwakeword models folder.")
		else:
			print(f"WARNING: {model_name} not found in lib/openwakeword/ — copy it there manually.")

	def setup_respeaker(self) -> None:
		"""Clone ReSpeaker XVF3800 host control tools into lib/respeaker/."""
		script_dir = os.path.dirname(os.path.abspath(__file__))
		respeaker_dir = os.path.join(script_dir, "lib", "respeaker")

		if not os.path.isdir(respeaker_dir):
			print("Cloning ReSpeaker XVF3800 repo...")
			subprocess.check_call([
				"git", "clone",
				"https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY.git",
				respeaker_dir
			])
		else:
			print("ReSpeaker repo already present, skipping clone.")

		print("ReSpeaker xvf_host ready.")

	def setup_pl2303(self) -> None:
		"""Build and install the pl2303 kernel module from source.

		The stock Tegra kernel does not include pl2303. We ship the driver
		source in lib/pl2303/src/ and build it against the running kernel's
		headers so it survives JetPack updates.
		"""
		import platform
		script_dir   = os.path.dirname(os.path.abspath(__file__))
		module_dir   = os.path.join(script_dir, "lib", "pl2303")
		kernel_ver   = platform.release()
		install_path = f"/lib/modules/{kernel_ver}/kernel/drivers/usb/serial/pl2303.ko"

		if not os.path.isdir(module_dir):
			print("lib/pl2303 not found — skipping pl2303 build.")
			return

		# Skip rebuild if module is already installed for this exact kernel
		if os.path.exists(install_path):
			print(f"pl2303: module already installed for {kernel_ver}, skipping build.")
		else:
			print(f"pl2303: building for kernel {kernel_ver}...")
			try:
				subprocess.check_call(["make", "-C", module_dir, "all"])
			except subprocess.CalledProcessError as e:
				print(f"pl2303: build failed — {e}")
				print("Ensure linux-headers are installed for the running kernel.")
				sys.exit(1)

			print(f"pl2303: installing to {install_path}...")
			subprocess.check_call([
				"sudo", "cp",
				os.path.join(module_dir, "src", "pl2303.ko"),
				install_path,
			])
			subprocess.check_call(["sudo", "depmod", "-a"])

		print("pl2303: loading modules...")
		subprocess.check_call(["sudo", "modprobe", "usbserial"])
		subprocess.check_call(["sudo", "modprobe", "pl2303"])

		print("pl2303: enabling on boot...")
		subprocess.check_call(
			"printf 'usbserial\\npl2303\\n' | sudo tee /etc/modules-load.d/pl2303.conf",
			shell=True,
		)

		# Add the real user (not root) to the dialout group so /dev/ttyUSB*
		# is accessible without sudo. SUDO_USER is set when running via sudo.
		real_user = os.environ.get("SUDO_USER", "kermit")
		subprocess.check_call(["sudo", "usermod", "-aG", "dialout", real_user])
		print(f"pl2303: added '{real_user}' to dialout group — re-login required to take effect.")

		print("pl2303: done.")

	def setup_bashrc(self) -> None:
		"""Add required environment variables to ~/.bashrc if not already present."""
		bashrc = os.path.expanduser("~/.bashrc")
		# The CUDA exports were needed by the whisper.cpp GPU build. sherpa-onnx
		# runs the int8 model on CPU (quantized ops don't map to the CUDA
		# execution provider), so nothing in the STT path needs these now.
		# They're kept because they're harmless and other tooling may rely on
		# them; safe to drop if you confirm nothing else does.
		exports = [
			"export PATH=/usr/local/cuda/bin:$PATH",
			"export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH",
			"export PYTHONPATH=/home/kermit/.local/lib/python3.10/site-packages:$PYTHONPATH",
		]
		with open(bashrc, "r") as f:
			current = f.read()
		with open(bashrc, "a") as f:
			for line in exports:
				if line not in current:
					f.write(f"\n{line}")
					print(f"Added to .bashrc: {line}")
		print("bashrc updated. Run 'source ~/.bashrc' or re-login to apply.")

	def _run_command(self, command: str) -> None:
		try:
			subprocess.check_call(command, shell=True)
		except subprocess.CalledProcessError as e:
			print(f"Command failed: {command}\nError: {e}")
			sys.exit(1)

if __name__ == "__main__":
	Setup()