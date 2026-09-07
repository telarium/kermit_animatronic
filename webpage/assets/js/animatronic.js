// kermit.js

const protocol = 'ws://';
const socketUrl = `${protocol}${document.domain}:${location.port}`;

bInvertHeadNod = false;

// Default transports: connect on polling, then upgrade to WebSocket. Do not
// pin this to ['websocket'] — the bundled client is 4.7.5, which has no
// fallback if the upgrade fails.
const socket = io.connect(socketUrl);

socket.on('connect', () => {
	socket.emit('onConnect', { data: "I'm connected!" });
	console.log(`Socket connected via ${socket.io.engine.transport.name}`);
	socket.io.engine.on('upgrade', (transport) => {
		console.log(`Socket upgraded to ${transport.name}`);
	});
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
	setupWifiPopupEvents();
	setupConfigPopupEvents();
	setupModeCheckboxes();
	setupSubmitTTS();
	setupShowSelectorEvents();
	setupShowControlButtons();
	setupPasswordEnterKey();
});

/**
 * Update the voice command status displayed on the page.
 * @param {string} id - The status identifier.
 * @param {string} value - The value associated with the status.
 */
function updateStatus(id, value) {
	let statusText = id + ": <span>" + value + "</span>";

	if( value.includes("Waiting for")) {
		const submitButton = document.getElementById('submitTTSButton');
		if (submitButton) {
			submitButton.disabled = false;
			submitButton.classList.remove('disabled');
		}
	} else if( value.includes("Listening")) {
		const submitButton = document.getElementById('submitTTSButton');
		if (submitButton) {
			submitButton.disabled = true;
			submitButton.classList.add('disabled');
		}
	} else if( id.includes("Responding")) {
		statusText = id + ": <span>Responding...</span>";
		populateTTSInput(value);
	}

	const statusElement = document.getElementById('voiceCommandStatus');
	if (statusElement) {
		statusElement.innerHTML = statusText;
	} else {
		console.warn('Voice Command Status element not found!');
	}
}

socket.on('statusUpdate', ({ id, value }) => updateStatus(id, value));

// -------------------------------------------------------------------------
// Config editor
// -------------------------------------------------------------------------

// Full config from config.cfg as { section: { key: value } }, as loaded by
// the server (WiFi is excluded server-side — it has its own popup).
let animatronicConfig = {};

// These live in the [LLM] section but are pinned to the top of the editor
// under friendlier labels. LLMContextShort is used only on the offline path.
const AI_CONTEXT_SECTION = 'LLM';
const AI_CONTEXT_KEY = 'LLMContext';
const AI_CONTEXT_SHORT_KEY = 'LLMContextShort';
const PINNED_CONTEXT_FIELDS = [
	{ key: AI_CONTEXT_SHORT_KEY, title: 'AI Context (Short)' },
	{ key: AI_CONTEXT_KEY, title: 'AI Context (Long)' },
];

// Config keys shown as a checkbox rather than a text box, matched lowercased.
// They save as "1"/"0"; TRUTHY_CONFIG_VALUES is what configparser reads as true.
const BOOLEAN_CONFIG_KEYS = new Set(['leddisable']);
const TRUTHY_CONFIG_VALUES = new Set(['1', 'true', 'yes', 'on']);

function isBooleanConfigKey(key) {
	return BOOLEAN_CONFIG_KEYS.has(String(key).toLowerCase());
}

function isTruthyConfigValue(value) {
	return TRUTHY_CONFIG_VALUES.has(String(value ?? '').trim().toLowerCase());
}

socket.on('configLoaded', (data) => {
	animatronicConfig = data || {};
	console.log('Config loaded:', animatronicConfig);
	// Only (re)render when the editor isn't open, so a broadcast from
	// another client can't stomp on fields being edited here.
	const popup = document.getElementById('configPopup');
	if (popup && popup.style.display !== 'none' && popup.style.display !== '') {
		return;
	}
	buildConfigEditor();
});

/**
 * Send config edits to the server, which writes them to config.cfg (local
 * and USB copies), re-applies them to the running system, and rebroadcasts.
 * @param {Object} updates - Partial config as { section: { key: value } }.
 */
function saveConfig(updates) {
	socket.emit('onConfigSave', updates);
}

socket.on('configSaveResult', (result) => {
	if (result && result.success) {
		console.log('Config saved.', result.warning || '');
		setConfigSaveStatus(result.warning ? result.warning : 'Saved!', 'success');
	} else {
		const error = result ? result.error : 'unknown error';
		console.error('Config save failed:', error);
		setConfigSaveStatus(`Save failed: ${error}`, 'error');
	}
});

/**
 * Ask the server to copy the local backup — config, shows and any Word
 * documents — onto an attached USB drive. Copying shows takes a while, so
 * the button is held disabled until the result comes back.
 */
function restoreBackupToUSB() {
	alert('Attempting to restore backed up config and show data to any attached USB drive. Please wait a few minutes...');

	const button = document.getElementById('restoreBackupButton');
	if (button) {
		button.disabled = true;
		button.classList.add('disabled');
	}

	setConfigSaveStatus('Restoring to USB drive…', '');
	socket.emit('onRestoreBackup');
}

socket.on('restoreBackupResult', (result) => {
	const button = document.getElementById('restoreBackupButton');
	if (button) {
		button.disabled = false;
		button.classList.remove('disabled');
	}

	if (result && result.success) {
		console.log('Backup restored.', result.message || '');
		setConfigSaveStatus(result.message || 'Restored to the USB drive.', 'success');
	} else {
		const message = result ? result.message : 'unknown error';
		console.error('Restore failed:', message);
		setConfigSaveStatus(`Restore failed: ${message}`, 'error');
	}
});

function setConfigSaveStatus(message, cls) {
	const status = document.getElementById('configSaveStatus');
	if (!status) return;
	status.textContent = message;
	status.className = cls || '';
}

/**
 * Rebuild the editor form from animatronicConfig. AI Context (LLMContext)
 * is pinned to the top as a textarea; every other section follows in
 * config-file order with one text input per key.
 */
function buildConfigEditor() {
	const container = document.getElementById('configEditor');
	if (!container) return;
	container.innerHTML = '';

	const sections = Object.keys(animatronicConfig);
	if (sections.length === 0) {
		container.innerHTML = '<p style="color: #fff; text-align: center;">No config loaded yet.</p>';
		return;
	}

	// AI Context fields on top
	const llmSection = animatronicConfig[AI_CONTEXT_SECTION];
	if (llmSection) {
		PINNED_CONTEXT_FIELDS.forEach(({ key, title }) => {
			if (!(key in llmSection)) return;
			appendConfigSectionTitle(container, title);
			appendConfigField(container, AI_CONTEXT_SECTION, key, llmSection[key], {
				textarea: true,
				label: null, // section title already says it
			});
		});
	}

	// Everything else, in the order the server sent it
	sections.forEach(section => {
		const keys = Object.keys(animatronicConfig[section]).filter(
			key => !(section === AI_CONTEXT_SECTION
				&& PINNED_CONTEXT_FIELDS.some(field => field.key === key))
		);
		if (keys.length === 0) return;

		appendConfigSectionTitle(container, section);
		keys.forEach(key => {
			appendConfigField(container, section, key, animatronicConfig[section][key], {
				checkbox: isBooleanConfigKey(key),
			});
		});
	});
}

function appendConfigSectionTitle(container, title) {
	const el = document.createElement('div');
	el.classList.add('config-section-title');
	el.textContent = title;
	container.appendChild(el);
}

function appendConfigField(container, section, key, value, { textarea = false, checkbox = false, label = key } = {}) {
	const field = document.createElement('div');
	field.classList.add('config-field');

	const inputId = `config-${section}-${key}`;

	function buildLabel() {
		const labelEl = document.createElement('label');
		labelEl.setAttribute('for', inputId);
		labelEl.textContent = label;
		return labelEl;
	}

	if (checkbox) {
		// Box first, then label — not the label-above layout the text inputs use.
		field.classList.add('config-field-check');
		const input = document.createElement('input');
		input.type = 'checkbox';
		input.id = inputId;
		input.checked = isTruthyConfigValue(value);
		input.dataset.configSection = section;
		input.dataset.configKey = key;
		field.appendChild(input);
		if (label) {
			field.appendChild(buildLabel());
		}
		container.appendChild(field);
		return;
	}

	if (label) {
		field.appendChild(buildLabel());
	}

	const input = document.createElement(textarea ? 'textarea' : 'input');
	if (!textarea) input.type = 'text';
	input.id = inputId;
	input.value = value;
	input.dataset.configSection = section;
	input.dataset.configKey = key;
	input.setAttribute('autocomplete', 'off');
	field.appendChild(input);

	container.appendChild(field);
}

/**
 * Collect edited fields (compared against the last loaded config) and send
 * only the changes to the server.
 */
function submitConfigSave() {
	const fields = document.querySelectorAll('#configEditor [data-config-section]');
	const updates = {};
	let changedCount = 0;

	fields.forEach(el => {
		const section = el.dataset.configSection;
		const key = el.dataset.configKey;
		const original = (animatronicConfig[section] || {})[key] ?? '';
		let value;

		if (el.type === 'checkbox') {
			// Compared as booleans: "1", "true" and "yes" all mean the same, so
			// an untouched box must not count as an edit by normalising to "1".
			if (el.checked === isTruthyConfigValue(original)) return;
			value = el.checked ? '1' : '0';
		} else {
			// Config values are single INI lines: fold any newlines into spaces.
			value = el.value.replace(/[\r\n]+/g, ' ').trim();
			if (value === original) return;
		}

		if (!updates[section]) updates[section] = {};
		updates[section][key] = value;
		changedCount++;
	});

	if (changedCount === 0) {
		setConfigSaveStatus('No changes to save.', '');
		return;
	}

	setConfigSaveStatus('Saving…', '');
	console.log('Saving config changes:', updates);
	saveConfig(updates);
}

function openConfigPopup() {
	buildConfigEditor();
	setConfigSaveStatus('', '');
	const popup = document.getElementById('configPopup');
	if (popup) {
		popup.style.display = 'flex';
	} else {
		console.warn('Config Popup element not found!');
	}
}

function closeConfigPopup() {
	const popup = document.getElementById('configPopup');
	if (popup) {
		popup.style.display = 'none';
	} else {
		console.warn('Config Popup element not found!');
	}
}

function setupConfigPopupEvents() {
	const indicator = document.getElementById('configIndicator');
	const closeButton = document.getElementById('closeConfigPopup');
	const saveButton = document.getElementById('saveConfigButton');
	const restoreButton = document.getElementById('restoreBackupButton');
	const popupOverlay = document.getElementById('configPopup');

	if (indicator) {
		indicator.addEventListener('keydown', (e) => {
			if (e.key === 'Enter' || e.key === ' ') {
				e.preventDefault();
				openConfigPopup();
			}
		});
	}

	if (closeButton) {
		closeButton.addEventListener('click', closeConfigPopup);
	} else {
		console.warn('Close Config Popup button not found!');
	}

	if (saveButton) {
		saveButton.addEventListener('click', submitConfigSave);
	} else {
		console.warn('Save Config Button not found!');
	}

	if (restoreButton) {
		restoreButton.addEventListener('click', restoreBackupToUSB);
	} else {
		console.warn('Restore Backup Button not found!');
	}

	if (popupOverlay) {
		popupOverlay.addEventListener('click', (e) => {
			if (e.target === popupOverlay) {
				closeConfigPopup();
			}
		});
	} else {
		console.warn('Config Popup overlay not found!');
	}
}

// -------------------------------------------------------------------------
// Show selection
// -------------------------------------------------------------------------

// The show list as the server sent it, the one currently "on deck", and the
// subset the popup is showing after the filter and sort are applied.
let showList = [];
let selectedShow = null;
let visibleShows = [];
let showSortAscending = true;

const NO_SHOW_SELECTED = '-- Select A Show! --';

/**
 * Fold a show name or a filter query into comparable form. Show names are
 * filename stems, so they carry punctuation and casing a typist won't
 * reproduce — "Gin & Juice - Snoop Frogg" has to match a typed "gin juice".
 * @param {string} text
 * @returns {string} - Lowercased, alphanumeric words separated by spaces.
 */
function normalizeShowText(text) {
	return String(text ?? '')
		.toLowerCase()
		.replace(/[&+]/g, ' and ')
		.replace(/[^a-z0-9]+/g, ' ')
		.trim();
}

/**
 * True if every filter token appears somewhere in the show name. Tokens are
 * ANDed, so typing a second word narrows rather than widens the list.
 * @param {string} show
 * @param {string[]} tokens - Already normalized.
 */
function showMatchesFilter(show, tokens) {
	if (tokens.length === 0) {
		return true;
	}
	const haystack = normalizeShowText(show);
	return tokens.every(token => haystack.includes(token));
}

/**
 * Update the on-deck display in the show card to match selectedShow.
 */
function updateShowOnDeck() {
	const onDeck = document.getElementById('showOnDeck');
	const label = document.getElementById('showOnDeckName');
	if (!onDeck || !label) {
		console.warn('Show on-deck element not found!');
		return;
	}
	label.textContent = selectedShow || NO_SHOW_SELECTED;
	onDeck.classList.toggle('empty', !selectedShow);
	onDeck.title = selectedShow
		? `On deck: ${selectedShow} — tap to change`
		: 'Choose a show';
}

/**
 * (Re)build the list inside the show popup from showList, applying the
 * current filter text and sort direction.
 */
function buildShowListPanel() {
	const container = document.getElementById('showListContainer');
	if (!container) {
		console.warn('Show list container not found!');
		return;
	}

	container.innerHTML = '';

	const filterInput = document.getElementById('showFilterInput');
	const tokens = normalizeShowText(filterInput ? filterInput.value : '')
		.split(' ')
		.filter(Boolean);

	visibleShows = showList.filter(show => showMatchesFilter(show, tokens));
	// numeric so "Track 2" sorts before "Track 10"; base sensitivity so
	// casing doesn't split the alphabet in two.
	visibleShows.sort((a, b) => {
		const order = a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
		return showSortAscending ? order : -order;
	});

	if (showList.length === 0 || visibleShows.length === 0) {
		const empty = document.createElement('p');
		empty.className = 'show-list-empty';
		empty.textContent = showList.length === 0
			? 'No shows available.'
			: 'No shows match that filter.';
		container.appendChild(empty);
		return;
	}

	visibleShows.forEach(show => {
		const item = document.createElement('div');
		item.className = 'show-item';
		if (show === selectedShow) {
			item.classList.add('selected');
		}
		item.dataset.show = show;
		item.setAttribute('role', 'button');
		item.setAttribute('tabindex', '0');
		// textContent, never innerHTML — show names come from filenames on a
		// USB drive and must never be able to inject markup.
		item.textContent = show;
		item.title = show;

		item.addEventListener('click', () => chooseShow(show));
		item.addEventListener('keydown', (e) => {
			if (e.key === 'Enter' || e.key === ' ') {
				e.preventDefault();
				chooseShow(show);
			}
		});

		container.appendChild(item);
	});
}

/**
 * Put a show on deck and close the panel. Loading happens on Play — this
 * only changes what Play will send.
 * @param {string} show
 */
function chooseShow(show) {
	selectedShow = show;
	updateShowOnDeck();
	closeShowPopup();
	console.log(`Show on deck: ${show}`);
}

function setShowSortAscending(ascending) {
	showSortAscending = !!ascending;
	localStorage.setItem('showSortAscending', showSortAscending);

	const label = document.getElementById('showSortLabel');
	if (label) {
		label.textContent = showSortAscending ? 'A–Z' : 'Z–A';
	}

	const button = document.getElementById('showSortButton');
	if (button) {
		button.classList.toggle('descending', !showSortAscending);
		button.title = showSortAscending
			? 'Sorted A to Z — tap for Z to A'
			: 'Sorted Z to A — tap for A to Z';
	}
}

function openShowPopup() {
	const popup = document.getElementById('showPopup');
	if (!popup) {
		console.warn('Show Popup element not found!');
		return;
	}

	// Always open on the full list — a filter left over from last time would
	// look like shows had gone missing.
	const filterInput = document.getElementById('showFilterInput');
	if (filterInput) {
		filterInput.value = '';
	}

	buildShowListPanel();
	popup.style.display = 'flex';

	// Bring the current pick into view, so reopening a long list doesn't
	// start at the top every time.
	const current = popup.querySelector('.show-item.selected');
	if (current) {
		current.scrollIntoView({ block: 'nearest' });
	}

	// Not on mobile: the on-screen keyboard would cover the list the user
	// opened the panel to look at.
	if (filterInput && !isMobileDevice()) {
		filterInput.focus();
	}
}

function closeShowPopup() {
	const popup = document.getElementById('showPopup');
	if (popup) {
		popup.style.display = 'none';
	} else {
		console.warn('Show Popup element not found!');
	}
}

function setupShowSelectorEvents() {
	setShowSortAscending(localStorage.getItem('showSortAscending') !== 'false');
	updateShowOnDeck();

	const onDeck = document.getElementById('showOnDeck');
	if (onDeck) {
		onDeck.addEventListener('keydown', (e) => {
			if (e.key === 'Enter' || e.key === ' ') {
				e.preventDefault();
				openShowPopup();
			}
		});
	} else {
		console.warn('Show on-deck element not found!');
	}

	const closeButton = document.getElementById('closeShowPopup');
	if (closeButton) {
		closeButton.addEventListener('click', closeShowPopup);
	} else {
		console.warn('Close Show Popup button not found!');
	}

	const sortButton = document.getElementById('showSortButton');
	if (sortButton) {
		sortButton.addEventListener('click', () => {
			setShowSortAscending(!showSortAscending);
			buildShowListPanel();
		});
	} else {
		console.warn('Show Sort Button not found!');
	}

	const filterInput = document.getElementById('showFilterInput');
	if (filterInput) {
		filterInput.addEventListener('input', buildShowListPanel);
		filterInput.addEventListener('keydown', (e) => {
			if (e.key === 'Escape') {
				e.preventDefault();
				// First Escape clears the filter, a second closes the panel.
				if (filterInput.value) {
					filterInput.value = '';
					buildShowListPanel();
				} else {
					closeShowPopup();
				}
			} else if (e.key === 'Enter') {
				// Filter down to what you want, hit Enter, done.
				e.preventDefault();
				if (visibleShows.length > 0) {
					chooseShow(visibleShows[0]);
				}
			}
		});
	} else {
		console.warn('Show Filter input not found!');
	}

	const popupOverlay = document.getElementById('showPopup');
	if (popupOverlay) {
		popupOverlay.addEventListener('click', (e) => {
			if (e.target === popupOverlay) {
				closeShowPopup();
			}
		});
	} else {
		console.warn('Show Popup overlay not found!');
	}
}

socket.on('showListLoaded', (data) => {
	showList = Array.isArray(data) ? data : [];
	console.log(`Show list loaded: ${showList.length} show(s)`);

	// A USB drive coming or going swaps the whole show directory, which can
	// take the on-deck show with it. Drop it rather than leaving Play
	// pointed at a show that no longer exists.
	if (selectedShow && !showList.includes(selectedShow)) {
		console.log(`Show '${selectedShow}' is no longer available, clearing.`);
		selectedShow = null;
	}

	updateShowOnDeck();
	buildShowListPanel();
});

// Handle play, pause, and stop button state based on show status
function updateShowButtons(status) {
	const playButton = document.getElementById('playButton');
	const pauseButton = document.getElementById('pauseButton');
	const stopButton = document.getElementById('stopButton');
	if (!playButton || !pauseButton || !stopButton) return;

	const setEnabled = (btn, enabled) => {
		btn.disabled = !enabled;
		btn.classList.toggle('disabled', !enabled);
	};

	if (status === 'play') {
		// Playing: can pause or stop, but not play again
		setEnabled(playButton, false);
		setEnabled(pauseButton, true);
		setEnabled(stopButton, true);
	} else if (status === 'pause') {
		// Paused: can play (resume) or stop, but not pause again
		setEnabled(playButton, true);
		setEnabled(pauseButton, false);
		setEnabled(stopButton, true);
	} else {
		// Stopped / ended / idle: can only play
		setEnabled(playButton, true);
		setEnabled(pauseButton, false);
		setEnabled(stopButton, false);
	}
}

socket.on('showStatusUpdated', (status) => updateShowButtons(status));

// Handle play, pause, and stop buttons for shows
function setupShowControlButtons() {
	const playButton = document.getElementById('playButton');
	const pauseButton = document.getElementById('pauseButton');
	const stopButton = document.getElementById('stopButton');

	if (playButton) {
		playButton.addEventListener('click', () => {
			if (!selectedShow) {
				// Nothing on deck — open the panel rather than just complaining.
				console.warn('No show selected.');
				openShowPopup();
				return;
			}
			socket.emit('showPlay', selectedShow);
			console.log(`Playing show: ${selectedShow}`);
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

// -------------------------------------------------------------------------
// Movement keypad grid
// -------------------------------------------------------------------------

// Movement descriptors from the character config, broadcast by the server on
// connect as [{ key, gamepad_buttons, description }]. Keys not in knownKeys
// are ignored by sendKey, so we never emit presses the animatronic can't use.
let keyMap = [];
const knownKeys = new Set();
const keyCells = new Map();   // lowercased key -> grid cell element

// Physical keyboard rows, each left-to-right. Movements are grouped by the row
// their key lives on, so keys from different keyboard rows never share a row in
// the grid.
const KEYBOARD_ROWS = ['1234567890', 'qwertyuiop', 'asdfghjkl', 'zxcvbnm'];

function keyPosition(key) {
	const k = String(key).toLowerCase();
	for (let row = 0; row < KEYBOARD_ROWS.length; row++) {
		const col = KEYBOARD_ROWS[row].indexOf(k);
		if (col !== -1) {
			return { row, col };
		}
	}
	// Unknown keys land in a trailing row, ordered by char code.
	return { row: KEYBOARD_ROWS.length, col: k.charCodeAt(0) || 0 };
}

socket.on('keyMapLoaded', (data) => {
	keyMap = Array.isArray(data) ? data : [];
	console.log('Key map loaded:', keyMap);
	buildKeyGrid();
});

// A movement fired (or released) on the device, from any source — keyboard,
// gamepad, MIDI, or show playback. Tint that key's description while it's
// active; the keycap invert stays tied to local key/pointer presses.
socket.on('movementKeyActivated', ({ key, on }) => {
	const cell = keyCells.get(String(key).toLowerCase());
	if (cell) {
		cell.classList.toggle('movement-on', !!on);
	}
});

/**
 * Build an SVG keycap resembling a physical keyboard key, with the given
 * label centered on its face. Colors are deliberately left to the CSS
 * (.keycap-* classes) so both the default look and the inverted/highlighted
 * look are fully themeable.
 * @param {string} label - Text to show on the key (e.g. "S").
 * @returns {string} - SVG markup.
 */
function buildKeycapSVG(label) {
	return `
		<svg class="keycap-svg" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" focusable="false">
			<rect class="keycap-body" x="5" y="5" width="90" height="90" rx="16" ry="16" />
			<rect class="keycap-face" x="15" y="12" width="70" height="63" rx="12" ry="12" />
			<line class="keycap-detail" x1="16" y1="90" x2="24" y2="82" />
			<line class="keycap-detail" x1="84" y1="90" x2="76" y2="82" />
			<text class="keycap-letter" x="50" y="44" text-anchor="middle" dominant-baseline="central">${label}</text>
		</svg>`;
}

/**
 * (Re)build the keypad grid from keyMap. Movements are grouped by their
 * physical keyboard row (top-to-bottom); within a row they're ordered
 * left-to-right. Each keyboard row becomes its own centered row of keys, so
 * keys from different keyboard rows never share a row here.
 */
function buildKeyGrid() {
	const grid = document.getElementById('keyGrid');
	if (!grid) {
		console.warn('Key grid container not found!');
		return;
	}

	grid.innerHTML = '';
	knownKeys.clear();
	keyCells.clear();

	// Bucket movements by keyboard-row index.
	const rows = new Map();   // rowIndex -> [{ key, description, col }]
	keyMap.filter(m => m && m.key).forEach(m => {
		const key = String(m.key).toLowerCase();
		const pos = keyPosition(key);
		if (!rows.has(pos.row)) {
			rows.set(pos.row, []);
		}
		rows.get(pos.row).push({ key, description: m.description || '', col: pos.col });
	});

	// Emit rows top-to-bottom, keys left-to-right within each.
	[...rows.keys()].sort((a, b) => a - b).forEach(rowIndex => {
		const rowEl = document.createElement('div');
		rowEl.className = 'key-row';

		rows.get(rowIndex).sort((a, b) => a.col - b.col).forEach(item => {
			knownKeys.add(item.key);

			const cell = document.createElement('div');
			cell.className = 'key-cell';
			cell.dataset.key = item.key;
			cell.setAttribute('role', 'button');
			cell.setAttribute('tabindex', '0');
			cell.setAttribute('aria-pressed', 'false');
			if (item.description) {
				cell.title = item.description;
			}

			// SVG keycap, then the description label below it. The description
			// is set via textContent so config text can never inject markup.
			cell.innerHTML = buildKeycapSVG(item.key.toUpperCase());
			const desc = document.createElement('span');
			desc.className = 'key-desc';
			desc.textContent = item.description;
			cell.appendChild(desc);

			// Pointer (mouse/touch/pen) drives the movement like a momentary key.
			cell.addEventListener('pointerdown', (e) => {
				e.preventDefault();
				pressPointerKey(item.key);
			});

			keyCells.set(item.key, cell);
			rowEl.appendChild(cell);
		});

		grid.appendChild(rowEl);
	});
}

/**
 * Toggle the inverted highlight on the keycap for a given key, if present.
 * @param {string} key
 * @param {boolean} pressed
 */
function highlightKey(key, pressed) {
	const cell = keyCells.get(String(key).toLowerCase());
	if (cell) {
		cell.classList.toggle('active', pressed);
		cell.setAttribute('aria-pressed', pressed ? 'true' : 'false');
	}
}

// Track the key currently held via pointer so we can release it on a pointerup
// anywhere on the page (even if the pointer drifts off the key beforehand).
let pointerHeldKey = null;

function pressPointerKey(key) {
	if (pointerHeldKey !== null) {
		releasePointerKey();
	}
	pointerHeldKey = key;
	sendKey(key, 1);
	highlightKey(key, true);
}

function releasePointerKey() {
	if (pointerHeldKey === null) {
		return;
	}
	sendKey(pointerHeldKey, 0);
	highlightKey(pointerHeldKey, false);
	pointerHeldKey = null;
}

document.addEventListener('pointerup', releasePointerKey);
document.addEventListener('pointercancel', releasePointerKey);

// Simplified key press handling
function sendKey(key, value) {
	const k = key.toLowerCase();
	// Only emit for keys that map to a movement in the loaded character
	// config — presses for any other key are ignored.
	if (!knownKeys.has(k)) {
		return;
	}
	if (bInvertHeadNod && k === 's') {
		value = 1 - value;
	}
	socket.emit('onKeyPress', { keyVal: k, val: value });
}

// Handle keyboard events
const down = new Set();

/**
 * True if the event originates from a form field (TTS input, WiFi password,
 * config editor fields, …). Typing there must never trigger movements.
 */
function isTypingTarget(event) {
	const tag = event.target.tagName;
	return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
}

function doKeyDown(event) {
	if (isTypingTarget(event)) {
		return;
	}

	const charCode = event.which || event.keyCode;
	if (!down.has(charCode)) {
		const key = String.fromCharCode(charCode);
		sendKey(key, 1);
		highlightKey(key, true);
		down.add(charCode);
	}
}

function doKeyUp(event) {
	if (isTypingTarget(event)) {
		return;
	}

	const charCode = event.which || event.keyCode;
	if (down.has(charCode)) {
		const key = String.fromCharCode(charCode);
		sendKey(key, 0);
		highlightKey(key, false);
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

// -------------------------------------------------------------------------
// Text submission (Repeat / Command)
// -------------------------------------------------------------------------

// Repeat: the animatronic speaks the text back verbatim.
// Command: the text is handed to the voice command / LLM path, exactly as a
// spoken utterance would be.
let bCommandMode = false;

function setupSubmitTTS() {
	const submitButton = document.getElementById('submitTTSButton');
	const ttsInput = document.getElementById('ttsInput');
	const repeatRadio = document.getElementById('textModeRepeat');
	const commandRadio = document.getElementById('textModeCommand');

	if (submitButton) {
		submitButton.addEventListener('click', submitText);
	} else {
		console.warn('Submit Text Button not found!');
	}

	if (ttsInput) {
		ttsInput.addEventListener('keydown', function (event) {
			if (event.key === 'Enter') {
				event.preventDefault();
				submitText();
			}
		});
	} else {
		console.warn('TTS Input field not found!');
	}

	if (repeatRadio && commandRadio) {
		if (localStorage.getItem('commandModeEnabled') === 'true') {
			commandRadio.checked = true;
		} else {
			repeatRadio.checked = true;
		}
		repeatRadio.addEventListener('change', () => setCommandMode(false));
		commandRadio.addEventListener('change', () => setCommandMode(true));
		setCommandMode(commandRadio.checked);
	} else {
		console.warn('Text mode toggle not found!');
		setCommandMode(false);
	}
}

function setCommandMode(enabled) {
	bCommandMode = !!enabled;
	localStorage.setItem('commandModeEnabled', bCommandMode);

	const inputField = document.getElementById('ttsInput');
	if (inputField) {
		inputField.placeholder = bCommandMode
			? 'Type something to ask…'
			: 'Type something to say…';
	}
}

function submitText() {
	const inputField = document.getElementById('ttsInput');
	const submitButton = document.getElementById('submitTTSButton');
	const inputText = inputField ? inputField.value.trim() : '';

	if (inputText) {
		console.log(`Submitted ${bCommandMode ? 'command' : 'text'}: ${inputText}`);
		if (submitButton) {
			submitButton.disabled = true;
			submitButton.classList.add('disabled');
		}

		socket.emit('onWebTextSubmit', { text: inputText, isCommand: bCommandMode });
	} else {
		console.warn('No text entered for submission.');
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

function signalToLevel(signal) {
	// Map signal strength (0-100%) to icon level, Windows-style:
	// 0 = disconnected, 1 = dot only, 2-4 = arcs lighting up
	if (signal >= 75) return 4;
	if (signal >= 50) return 3;
	if (signal >= 25) return 2;
	if (signal > 0)   return 1;
	return 1; // connected but 0% reported — still show the dot
}

function updateWifiIndicator(ssid, signal) {
	const indicator = document.getElementById('wifiIndicator');
	const label = document.getElementById('wifiSSIDLabel');
	if (!indicator || !label) return;
	if (ssid) {
		indicator.dataset.level = signalToLevel(signal);
		indicator.title = `${ssid} (${signal}%) — WiFi settings`;
		label.textContent = truncateString(ssid, 28);
	} else {
		indicator.dataset.level = 0;
		indicator.title = 'WiFi settings';
		label.textContent = 'Not connected';
	}
}

socket.on('wifiScan', function(data) {
	wifiSSIDs = data;
});

socket.on('wifiConnected', function(data) {
	updateWifiIndicator(data.ssid, data.signal);
});

socket.on('wifiDisconnected', function() {
	updateWifiIndicator(null, 0);
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
	const wifiIndicator = document.getElementById('wifiIndicator');

	if (wifiIndicator) {
		wifiIndicator.addEventListener('keydown', (e) => {
			if (e.key === 'Enter' || e.key === ' ') {
				e.preventDefault();
				openWifiPopup();
			}
		});
	}

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
