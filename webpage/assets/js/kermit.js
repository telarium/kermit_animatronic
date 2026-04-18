// kermit.js

const protocol = 'ws://';
const socketUrl = `${protocol}${document.domain}:${location.port}`;

bInvertHeadNod = false;

// Connect to the server using polling transport instead of WebSocket
const socket = io.connect(socketUrl, { transports: ['polling'] });

socket.on('connect', () => {
	socket.emit('onConnect', { data: "I'm connected!" });
});

/**
 * Truncate a string to a specified maximum length, adding ellipsis if truncated.
 * @param {string} str - The string to truncate.
 * @param {number} maxLength - The maximum allowed length of the string.
 * @returns {string} - The truncated string with ellipsis if needed.
 */
function truncateString(str, maxLength) {
	return str.length > maxLength ? `${str.slice(0, maxLength - 3)}...` : str;
}

/**
 * Detect if the current device is a mobile device (Android/iOS).
 * @returns {boolean} - True if mobile device, else false.
 */
function isMobileDevice() {
	const ua = navigator.userAgent.toLowerCase();
	return /android|iphone|ipad|ipod/.test(ua);
}

// Consolidated DOMContentLoaded Event Listener
document.addEventListener('DOMContentLoaded', () => {
	handleMobileKeypadVisibility();
	setupWifiPopupEvents();
	setupModeCheckboxes();
	setupSubmitTTS();
	setupShowControlButtons();
	setupPasswordEnterKey();
});

// Handle visibility of keypad images on mobile devices
function handleMobileKeypadVisibility() {
	if (!isMobileDevice()) return;

	const keypadImages = ['images/keypad-l.png', 'images/keypad-r.png'];
	keypadImages.forEach(src => {
		const img = document.querySelector(`img[src="${src}"]`);
		if (img) {
			const container = img.closest('.box');
			if (container) container.style.display = 'none';
		}
	});
}

/**
 * Update the voice command status displayed on the page.
 * @param {string} id - The status identifier.
 * @param {string} value - The value associated with the status.
 */
function updateVoiceCommandStatus(id, value) {
	let statusText = "";

	switch (id) {
		case "idle":
			statusText = "Waiting for 'Hey Kermit'...";
			break;
		case "wakeWord":
			statusText = "Listening...";
			break;
		case "command":
			statusText = `Executing command '${value}'`;
			break;
		case "transcribing":
			statusText = "Transcribing...";
			break;
		case "llmSend":
			statusText = `Heard '${value}'`;
			break;
		case "llmReceive":
			statusText = "Responding...";
			populateTTSInput(value);
			break;
		case "micNotFound":
			statusText = "No microphone detected";
			break;
		case "error":
			statusText = "Disabled!";
			break;
		case "ttsSubmitted":
			statusText = "Processing...";
			break;
		case "speaking":
			statusText = "Responding...";
			break;
		case "ttsComplete":
			statusText = "Waiting...";
			const submitButton = document.getElementById('submitTTSButton');
			if (submitButton) {
				submitButton.disabled = false;
				submitButton.classList.remove('disabled');
			}
			break;
		default:
			statusText = "Unknown status: " + id;
	}

	const statusElement = document.getElementById('voiceCommandStatus');
	if (statusElement) {
		statusElement.innerHTML = `Voice Command Status: <span>${statusText}</span>`;
	} else {
		console.warn('Voice Command Status element not found!');
	}
}

socket.on('voiceCommandUpdate', ({ id, value }) => updateVoiceCommandStatus(id, value));

// Handle show list loading
let showList = [];
socket.on('showListLoaded', (data) => {
	showList = ["-- Select A Show! --", ...data];

	const dropdown = document.querySelector('select[name="Show List"]');
	if (dropdown) {
		dropdown.innerHTML = '';

		showList.forEach(item => {
			const option = document.createElement('option');
			option.value = item;
			option.textContent = item;
			dropdown.appendChild(option);
		});
	} else {
		console.warn('Show List dropdown not found!');
	}
});

// Handle play, pause, and stop buttons for shows
function setupShowControlButtons() {
	const playButton = document.getElementById('playButton');
	const pauseButton = document.getElementById('pauseButton');
	const stopButton = document.getElementById('stopButton');

	if (playButton) {
		playButton.addEventListener('click', () => {
			const dropdown = document.getElementById('showListDropdown');
			const selectedShow = dropdown ? dropdown.value : null;

			if (selectedShow) {
				if (dropdown.selectedIndex === 0) {
					alert('Mama mia! Please select a show first!');
				} else {
					socket.emit('showPlay', selectedShow);
					console.log(`Playing show: ${selectedShow}`);
				}
			} else {
				console.warn('No show selected.');
			}
		});
	} else {
		console.warn('Play Button not found!');
	}

	if (pauseButton) {
		pauseButton.addEventListener('click', () => {
			socket.emit('showPause');
			console.log('Pausing show');
		});
	} else {
		console.warn('Pause Button not found!');
	}

	if (stopButton) {
		stopButton.addEventListener('click', () => {
			socket.emit('showStop');
			console.log('Stopping show');
		});
	} else {
		console.warn('Stop Button not found!');
	}
}

// Simplified key press handling
function sendKey(key, value) {
	if (bInvertHeadNod && key.toLowerCase() === 's') {
		value = 1 - value;
	}
	socket.emit('onKeyPress', { keyVal: key.toLowerCase(), val: value });
}

// Handle keyboard events
const down = new Set();

function doKeyDown(event) {
	if (event.target.id === 'ttsInput' || event.target.id === 'wifiPassword') {
		return;
	}

	const charCode = event.which || event.keyCode;
	if (!down.has(charCode)) {
		sendKey(String.fromCharCode(charCode), 1);
		down.add(charCode);
	}
}

function doKeyUp(event) {
	if (event.target.id === 'ttsInput' || event.target.id === 'wifiPassword') {
		return;
	}

	const charCode = event.which || event.keyCode;
	if (down.has(charCode)) {
		sendKey(String.fromCharCode(charCode), 0);
		down.delete(charCode);
	}
}

document.addEventListener('keydown', doKeyDown);
document.addEventListener('keyup', doKeyUp);

// Mode Handling
function setupModeCheckboxes() {
	const mirroredModeCheckbox = document.getElementById('mirroredModeCheckbox');
	const retroModeCheckbox = document.getElementById('retroModeCheckbox');
	headNodInvertedCheckbox = document.getElementById('headNodInvertedCheckbox');

	if (mirroredModeCheckbox) {
		const mirroredModeEnabled = localStorage.getItem('mirroredModeEnabled') === 'true';
		mirroredModeCheckbox.checked = mirroredModeEnabled;

		mirroredModeCheckbox.addEventListener('change', function () {
			bMirroredModeEnabled = this.checked;
			localStorage.setItem('mirroredModeEnabled', this.checked);
			socket.emit('onMirroredMode', this.checked);

			if (this.checked) {
				performFlipAnimation();
			} else {
				reverseFlipAnimation();
			}

			console.log(`Mirrored Mode is now ${this.checked ? 'Enabled' : 'Disabled'}`);
		});
	} else {
		console.warn('Mirrored Mode Checkbox not found!');
	}

	if (retroModeCheckbox) {
		const retroModeEnabled = localStorage.getItem('retroModeEnabled') === 'true';
		retroModeCheckbox.checked = retroModeEnabled;

		if (retroModeEnabled) {
			const mainContent = document.getElementById('main');
			if (mainContent) {
				mainContent.classList.add('retro-mode-active');
			}
		}

		retroModeCheckbox.addEventListener('change', function () {
			const mainContent = document.getElementById('main');
			if (mainContent) {
				if (this.checked) {
					mainContent.classList.add('retro-mode-active');
				} else {
					mainContent.classList.remove('retro-mode-active');
				}
				socket.emit('onRetroMode', this.checked);
				localStorage.setItem('retroModeEnabled', this.checked);
			} else {
				console.warn('Main content element not found!');
			}
		});
	} else {
		console.warn('Retro Mode Checkbox not found!');
	}

	if (headNodInvertedCheckbox) {
		bHeadInvertedEnabled = localStorage.getItem('headInvertedEnabled') === 'true';
		headNodInvertedCheckbox.checked = bHeadInvertedEnabled;

		headNodInvertedCheckbox.addEventListener('change', function () {
			bHeadInvertedEnabled = this.checked;
			localStorage.setItem('headInvertedEnabled', this.checked);
			socket.emit('onHeadNodInverted', this.checked);
			bInvertHeadNod = this.checked;
			socket.emit('onKeyPress', { keyVal: 's', val: Number(bInvertHeadNod) });
		});
	} else {
		console.warn('Head Inverted Checkbox not found!');
	}
}

function performFlipAnimation() {
	const mainContent = document.getElementById('main');
	if (!mainContent) {
		console.warn('Main content element not found!');
		return;
	}

	mainContent.classList.add('flip-animation');

	const removeAnimation = () => {
		mainContent.classList.remove('flip-animation');
		mainContent.removeEventListener('animationend', removeAnimation);
	};

	mainContent.addEventListener('animationend', removeAnimation);
}

function reverseFlipAnimation() {
	const mainContent = document.getElementById('main');
	if (mainContent) {
		console.log('Reverse flip animation triggered.');
	} else {
		console.warn('Main content element not found!');
	}
}

// Submit TTS Handling
function setupSubmitTTS() {
	const submitButton = document.getElementById('submitTTSButton');
	const ttsInput = document.getElementById('ttsInput');

	if (submitButton) {
		submitButton.addEventListener('click', submitTTS);
	} else {
		console.warn('Submit TTS Button not found!');
	}

	if (ttsInput) {
		ttsInput.addEventListener('keydown', function (event) {
			if (event.key === 'Enter') {
				event.preventDefault();
				submitTTS();
			}
		});
	} else {
		console.warn('TTS Input field not found!');
	}
}

function submitTTS() {
	const inputField = document.getElementById('ttsInput');
	const submitButton = document.getElementById('submitTTSButton');
	const inputText = inputField ? inputField.value.trim() : '';

	if (inputText) {
		console.log(`Submitted TTS Text: ${inputText}`);
		if (submitButton) {
			submitButton.disabled = true;
			submitButton.classList.add('disabled');
		}

		socket.emit('onWebTTSSubmit', inputText);
	} else {
		console.warn('No text entered for TTS submission.');
	}

	if (inputField) {
		inputField.value = '';
	}
}

function populateTTSInput(text) {
	const inputField = document.getElementById('ttsInput');
	if (inputField) {
		inputField.value = text;
		console.log(`Populated TTS Input with: ${text}`);
	} else {
		console.warn('TTS Input field not found!');
	}
}

// -------------------------------------------------------------------------
// WiFi
// -------------------------------------------------------------------------

let wifiSSIDs = [];
let selectedSSID = null;

function updateWifiStatusLink(ssid, signal) {
	const link = document.getElementById('wifiStatusLink');
	if (!link) return;
	if (ssid) {
		link.textContent = `${truncateString(ssid, 40)} (${signal}%)`;
	} else {
		link.textContent = 'Not connected';
	}
}

socket.on('wifiScan', function(data) {
	wifiSSIDs = data;
});

socket.on('wifiConnected', function(data) {
	updateWifiStatusLink(data.ssid, data.signal);
});

socket.on('wifiDisconnected', function() {
	updateWifiStatusLink(null, 0);
});

socket.on('wifiPasswordRequired', function(data) {
	alert(`Password required to connect to '${data.ssid}'. Please enter it in the WiFi popup.`);
	openWifiPopup();
});

socket.on('wifiWrongPassword', function(data) {
	alert(`Wrong password for '${data.ssid}'. Please try again.`);
	openWifiPopup();
});

function setupWifiPopupEvents() {
	const closePopupButton = document.getElementById('closeWifiPopup');
	const connectButton = document.getElementById('connectWifiButton');
	const popupOverlay = document.getElementById('wifiPopup');

	if (closePopupButton) {
		closePopupButton.addEventListener('click', closeWifiPopup);
	} else {
		console.warn('Close WiFi Popup button not found!');
	}

	if (connectButton) {
		connectButton.addEventListener('click', () => {
			const passwordInput = document.getElementById('wifiPassword');
			const password = passwordInput ? passwordInput.value.trim() : '';

			if (selectedSSID) {
				connectToWifi(selectedSSID, password);
				closeWifiPopup();
			} else {
				alert('Please select a WiFi network first.');
			}
		});
	} else {
		console.warn('Connect WiFi Button not found!');
	}

	if (popupOverlay) {
		popupOverlay.addEventListener('click', (e) => {
			if (e.target === popupOverlay) {
				closeWifiPopup();
			}
		});
	} else {
		console.warn('WiFi Popup overlay not found!');
	}
}

function openWifiPopup() {
	populateWifiList();
	const popup = document.getElementById('wifiPopup');
	if (popup) {
		popup.style.display = 'flex';
	} else {
		console.warn('WiFi Popup element not found!');
	}
}

function closeWifiPopup() {
	const popup = document.getElementById('wifiPopup');
	if (popup) {
		popup.style.display = 'none';
	} else {
		console.warn('WiFi Popup element not found!');
	}
}

function populateWifiList() {
	const wifiListDiv = document.getElementById('wifiList');
	if (!wifiListDiv) {
		console.warn('WiFi List container not found!');
		return;
	}

	wifiListDiv.innerHTML = '';

	if (wifiSSIDs.length === 0) {
		wifiListDiv.innerHTML = '<p style="color: #fff; text-align: center;">No WiFi networks found.</p>';
		return;
	}

	wifiSSIDs.forEach(ap => {
		const wifiItem = document.createElement('div');
		wifiItem.classList.add('wifi-item');
		wifiItem.dataset.ssid = ap.ssid;
		wifiItem.innerHTML = `
			<span>${ap.ssid}</span>
			<span>${ap.signal_strength}%</span>
		`;
		wifiItem.addEventListener('click', () => selectWifi(ap.ssid));
		wifiListDiv.appendChild(wifiItem);
	});
}

function selectWifi(ssid) {
	selectedSSID = ssid;
	const wifiItems = document.querySelectorAll('.wifi-item');
	wifiItems.forEach(item => {
		item.style.backgroundColor = item.dataset.ssid === ssid ? 'rgba(255, 255, 255, 0.2)' : '';
	});
}

function connectToWifi(ssid, password) {
	socket.emit('onConnectToWifi', { ssid, password });
	console.log(`Connecting to WiFi SSID: ${ssid}`);
}

function setupPasswordEnterKey() {
	const wifiPasswordInput = document.getElementById('wifiPassword');
	const connectButton = document.getElementById('connectWifiButton');

	if (wifiPasswordInput && connectButton) {
		wifiPasswordInput.addEventListener('keydown', function (event) {
			if (event.key === 'Enter') {
				event.preventDefault();
				if (selectedSSID) {
					connectButton.click();
				} else {
					alert('Please select a WiFi network first.');
				}
			}
		});
	} else {
		console.warn('WiFi Password input or Connect button not found!');
	}
}
