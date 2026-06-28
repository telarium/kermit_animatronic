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
			"pyusb", "webrtcvad",
			# whisper/STT
			"pyaudio",
			# USB-serial for ProgramBlue / PL2303 adapter
			"pyserial",
		])
		self.setup_piper_models()
		self.setup_whisper_models()
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
		subprocess.check_call(["sudo", "/usr/bin/python", "-m", "pip", "install", "--upgrade", "pip"])
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

	def setup_whisper_models(self) -> None:
		"""Download whisper ggml models into lib/whisper/models/."""
		script_dir = os.path.dirname(os.path.abspath(__file__))
		whisper_dir = os.path.join(script_dir, "lib", "whisper")
		models_dir  = os.path.join(whisper_dir, "models")
		download_script = os.path.join(models_dir, "download-ggml-model.sh")

		if not os.path.isdir(whisper_dir):
			print("lib/whisper not found - skipping model download.")
			return

		for model in ["tiny.en", "base.en"]:
			model_file = os.path.join(models_dir, f"ggml-{model}.bin")
			if os.path.exists(model_file):
				print(f"Model {model} already present, skipping.")
				continue
			print(f"Downloading whisper model: {model}")
			try:
				subprocess.check_call(
					["bash", download_script, model],
					cwd=whisper_dir
				)
			except subprocess.CalledProcessError as e:
				print(f"Failed to download whisper model {model}: {e}")
				sys.exit(1)

	def setup_openwakeword_models(self) -> None:
		"""Download openWakeWord base models and install custom hey_kermit model."""
		try:
			import openwakeword
			openwakeword.utils.download_models()
			print("openWakeWord base models downloaded.")
		except Exception as e:
			print(f"Failed to download openWakeWord models: {e}")
			sys.exit(1)

		# Copy custom hey_kermit model into the openwakeword resources folder
		script_dir = os.path.dirname(os.path.abspath(__file__))
		src = os.path.join(script_dir, "lib", "openwakeword", "hey_ker_mit.onnx")
		dst = "/usr/local/lib/python3.10/dist-packages/openwakeword/resources/models/hey_ker_mit.onnx"
		if os.path.exists(src):
			subprocess.check_call(["sudo", "cp", src, dst])
			print("hey_ker_mit.onnx installed to openwakeword models folder.")
		else:
			print("WARNING: hey_ker_mit.onnx not found in lib/openwakeword/ — copy it there manually.")

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