#!/usr/bin/env python3
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import List, Optional

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
		self.setup_sherpa_models()
		self.setup_llama()
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

	# Streaming transducer used by speech_to_text.py.
	SHERPA_MODEL = "sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25"
	SHERPA_MODEL_URL = (
		"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
		f"{SHERPA_MODEL}.tar.bz2"
	)

	def setup_sherpa_models(self) -> None:
		"""Download the Nemotron streaming transducer into lib/sherpa_onnx/models/.
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

	# llama.cpp moves fast and flag behaviour changes between releases, so
	# this is pinned rather than tracking master.
	LLAMA_REPO = "https://github.com/ggml-org/llama.cpp.git"
	LLAMA_COMMIT = "b96806d96"
	LLAMA_MODEL_REPO = "ggml-org/Qwen3-1.7B-GGUF"
	# Tried in order. The repo does not necessarily carry every quant, so the
	# actual filename is discovered from the HF API rather than hardcoded.
	LLAMA_QUANT_PREFERENCE = ["Q4_K_M", "Q8_0", "Q4_0", "BF16", "f16"]
	# Sanity floor for "is this actually a model?". The 1.7B Q4_K_M is ~1.2GB;
	# llama.cpp's own ggml-vocab-*.gguf test fixtures are 600KB-16MB, and a
	# wget that lands on an HTML error page is smaller still.
	LLAMA_MODEL_MIN_BYTES = 500 * 1024 * 1024
	LLAMA_PORT = 8081  # 8080 is whisper-server

	def _find_cuda_root(self) -> Optional[str]:
		"""Locate the CUDA toolkit. Deliberately does NOT rely on PATH: the
		exports setup_bashrc writes don't apply to the running process, and on
		JetPack the toolkit is always under /usr/local."""
		nvcc = shutil.which("nvcc")
		if nvcc:
			return os.path.dirname(os.path.dirname(nvcc))
		candidates = sorted(glob.glob("/usr/local/cuda*/bin/nvcc"), reverse=True)
		if candidates:
			return os.path.dirname(os.path.dirname(candidates[0]))
		return None

	def _cuda_env(self, cuda_root: str) -> dict:
		env = os.environ.copy()
		env["PATH"] = f"{cuda_root}/bin:" + env.get("PATH", "")
		env["LD_LIBRARY_PATH"] = f"{cuda_root}/lib64:" + env.get("LD_LIBRARY_PATH", "")
		return env

	def setup_llama(self) -> None:
		"""Build llama.cpp with CUDA into lib/llama/, fetch the GGUF, and
		install the llama-server unit. The build takes 10-15 minutes."""
		script_dir = os.path.dirname(os.path.abspath(__file__))
		llama_dir  = os.path.join(script_dir, "lib", "llama")
		binary     = os.path.join(llama_dir, "build", "bin", "llama-server")

		cuda_root = self._find_cuda_root()
		if cuda_root is None:
			print("llama: CUDA toolkit not found under /usr/local. Install it "
			      "(sudo apt install cuda-toolkit) and re-run.")
			return
		print(f"llama: using CUDA at {cuda_root}")

		if os.path.isfile(binary):
			print("llama.cpp already built, skipping build.")
		else:
			self._build_llama(llama_dir, cuda_root)

		# NOT lib/llama/models/. The llama.cpp checkout ships ~25 vocab-only
		# ggml-vocab-*.gguf test fixtures in that directory; treating them as
		# candidate models makes the download step no-op and hands systemd a
		# file with no weights, which fails at load with the fairly opaque
		# "check_tensor_dims: tensor 'token_embd.weight' not found".
		model_path = self._download_llama_model(os.path.join(script_dir, "models"))
		if model_path is None:
			print("llama: no model available — skipping service install.")
			return

		self._install_llama_service(binary, model_path, cuda_root)

	def _build_llama(self, llama_dir: str, cuda_root: str) -> None:
		if not os.path.isdir(llama_dir):
			print("Cloning llama.cpp...")
			subprocess.check_call(["git", "clone", self.LLAMA_REPO, llama_dir])
		subprocess.check_call(["git", "-C", llama_dir, "fetch", "--all", "--tags"])
		subprocess.check_call(["git", "-C", llama_dir, "checkout", self.LLAMA_COMMIT])

		env = self._cuda_env(cuda_root)
		print("Building llama.cpp with CUDA (10-15 minutes)...")
		try:
			subprocess.check_call(
				["cmake", "-B", "build", "-DGGML_CUDA=ON",
				 f"-DCMAKE_CUDA_COMPILER={cuda_root}/bin/nvcc"],
				cwd=llama_dir, env=env,
			)
			subprocess.check_call(
				["cmake", "--build", "build", "--config", "Release", "-j6"],
				cwd=llama_dir, env=env,
			)
		except subprocess.CalledProcessError as e:
			print(f"llama: build failed: {e}")
			sys.exit(1)

	def _existing_llama_model(self, models_dir: str) -> Optional[str]:
		"""Return a usable .gguf already in models_dir, or None.

		Sorted for determinism — os.listdir order is arbitrary, so an
		unsorted first-match is not reproducible between machines.
		"""
		for name in sorted(os.listdir(models_dir)):
			if not name.endswith(".gguf"):
				continue
			path = os.path.join(models_dir, name)
			size = os.path.getsize(path)
			if size >= self.LLAMA_MODEL_MIN_BYTES:
				print(f"llama: model already present ({name}), skipping download.")
				return path
			print(f"llama: ignoring {name} — {size} bytes is too small to be a model.")
		return None

	def _download_llama_model(self, models_dir: str) -> Optional[str]:
		os.makedirs(models_dir, exist_ok=True)

		existing = self._existing_llama_model(models_dir)
		if existing:
			return existing

		try:
			url = f"https://huggingface.co/api/models/{self.LLAMA_MODEL_REPO}"
			with urllib.request.urlopen(url, timeout=30) as response:
				data = json.load(response)
		except Exception as e:
			print(f"llama: could not list {self.LLAMA_MODEL_REPO}: {e}")
			return None

		available = [
			s["rfilename"] for s in data.get("siblings", [])
			if s.get("rfilename", "").endswith(".gguf")
		]
		if not available:
			print(f"llama: no .gguf files found in {self.LLAMA_MODEL_REPO}.")
			return None

		filename = None
		for quant in self.LLAMA_QUANT_PREFERENCE:
			match = next((f for f in available if quant.lower() in f.lower()), None)
			if match:
				filename = match
				break
		if filename is None:
			filename = available[0]
			print(f"llama: no preferred quant in {available}, using {filename}.")

		path = os.path.join(models_dir, os.path.basename(filename))
		url = f"https://huggingface.co/{self.LLAMA_MODEL_REPO}/resolve/main/{filename}"
		print(f"Downloading {filename} (~1.2GB)...")
		try:
			subprocess.check_call(["wget", "-O", path, url])
		except subprocess.CalledProcessError as e:
			print(f"llama: model download failed: {e}")
			if os.path.exists(path):
				os.remove(path)
			return None

		# wget exits 0 on a served error page, so check_call alone proves
		# nothing about what actually landed on disk.
		size = os.path.getsize(path)
		if size < self.LLAMA_MODEL_MIN_BYTES:
			print(f"llama: downloaded {filename} is only {size} bytes — expected ~1.2GB. "
			      "The server likely returned an error page rather than the model.")
			os.remove(path)
			return None

		print(f"llama: model installed to {path}.")
		return path

	# The unit is generated here rather than shipped as a file: every value in
	# it is install-specific, and lib/ is gitignored so a template couldn't
	# live next to the build it describes.
	LLAMA_SERVICE_NAME = "llama-server.service"
	LLAMA_SERVICE_UNIT = """[Unit]
Description=llama-server (offline LLM fallback for the animatronic)
After=network.target

[Service]
Type=simple
User={user}
# systemd does not source .bashrc, so CUDA has to be on the path here or the
# CUDA build fails to load libcudart at runtime.
Environment=PATH={cuda_root}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=LD_LIBRARY_PATH={cuda_root}/lib64
ExecStart={binary} -m {model_path} -ngl 99 -c 2048 --jinja --host 127.0.0.1 --port {port}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

	def _install_llama_service(self, binary: str, model_path: str, cuda_root: str) -> None:
		unit = self.LLAMA_SERVICE_UNIT.format(
			user=os.environ.get("SUDO_USER", "kermit"),
			cuda_root=cuda_root,
			binary=binary,
			model_path=model_path,
			port=self.LLAMA_PORT,
		)

		staged = os.path.join(tempfile.gettempdir(), self.LLAMA_SERVICE_NAME)
		destination = f"/etc/systemd/system/{self.LLAMA_SERVICE_NAME}"
		try:
			with open(staged, "w") as f:
				f.write(unit)
			subprocess.check_call(["sudo", "cp", staged, destination])
			subprocess.check_call(["sudo", "systemctl", "daemon-reload"])
			# Re-run replaces the unit, so restart rather than start — enable
			# --now is a no-op on an already-running service and would leave
			# the old paths live.
			subprocess.check_call(["sudo", "systemctl", "enable", self.LLAMA_SERVICE_NAME])
			subprocess.check_call(["sudo", "systemctl", "restart", self.LLAMA_SERVICE_NAME])
		except (subprocess.CalledProcessError, OSError) as e:
			print(f"llama: could not install the service: {e}")
			return
		finally:
			if os.path.exists(staged):
				os.remove(staged)

		# A unit that fails to load its model exits after ~1s and is then
		# restarted, so an immediate is-active check reports "activating" and
		# looks like success. Model load itself takes a few seconds. Wait past
		# both before believing it.
		print("llama: waiting for the service to settle...")
		time.sleep(20)
		result = subprocess.run(
			["systemctl", "is-active", self.LLAMA_SERVICE_NAME],
			capture_output=True, text=True,
		)
		if result.stdout.strip() != "active":
			print(f"llama: service is not running (state: {result.stdout.strip()}). "
			      "Check: journalctl -u llama-server -n 40")
			return

		print(f"llama-server installed at {destination} and running on "
		      f"127.0.0.1:{self.LLAMA_PORT}.")

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
		# The CUDA exports are required by the llama.cpp build in setup_llama —
		# without them cmake won't find nvcc. Nothing in the STT path needs
		# them any more (sherpa-onnx runs the int8 model on CPU), so do not
		# drop them on that basis. llama-server itself gets its CUDA paths
		# from the systemd unit, not from here.
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
