import gzip
import json
import os
import socket
import threading
import logging
from flask import Flask, jsonify, request, Response, send_from_directory
from flask_socketio import SocketIO
from pydispatch import dispatcher
from typing import Any
import utils
from logger import get_logger

log = get_logger(__name__)


_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Turn off extra log messages
# Named distinctly: plain `log` is this module's own logger, above.
_werkzeug_log = logging.getLogger('werkzeug')
_werkzeug_log.setLevel(logging.ERROR)

app = Flask(__name__, static_folder='webpage')
app.config['SECRET_KEY'] = 'Monkey Island is an amusement park.'

# Character-specific HTML values, injected into index.html at serve time.
# Overridden via the "html" section of the hardware JSON (see WebServer.__init__).
app.config['HTML_TITLE'] = 'Animatronic Controller'
app.config['CSS_FILE']   = 'assets/css/kermit.css'

# Uploaded shows are read into memory before being written out, so cap a
# request at a size a long song comfortably fits inside.
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024 * 1024
# Set by WebServer.set_upload_handler once the show uploader exists.
app.config['UPLOAD_HANDLER'] = None
# Group-show endpoints, set by WebServer.set_peer_handlers.
app.config['PEER_INFO_HANDLER'] = None
app.config['PEER_PUSH_HANDLER'] = None
app.config['PEER_SESSION_HANDLER'] = None
app.config['PEER_SPEECH_HANDLER'] = None

# Use threading mode for async
socketio = SocketIO(app, async_mode='threading', ping_timeout=30, logger=False, engineio_logger=False)


class WebServer:
	@app.route("/")
	def index() -> Response:
		"""Serve index.html with character-specific placeholders filled in."""
		index_path = os.path.join(app.static_folder, 'index.html')
		with open(index_path, 'r', encoding='utf-8') as f:
			html = f.read()
		html = html.replace('%%HTML_TITLE%%', app.config['HTML_TITLE'])
		html = html.replace('%%CSS_FILE%%', app.config['CSS_FILE'])
		return Response(html, mimetype='text/html')

	def broadcast(self, signal_id: str, data: Any) -> None:
		with app.app_context():
			try:
				socketio.emit(signal_id, data)
			except Exception as e:
				log.exception(f"Broadcast error: {e}")

	@app.route('/<path:path>')
	def static_proxy(path: str) -> Response:
		return app.send_static_file(path)

	@app.route('/help')
	def help_document() -> Response:
		"""Serve the documentation from docs/, which sits outside the web root.
		Sent inline, so the browser renders the PDF in the tab that opened it."""
		docs_dir = os.path.join(_BASE_DIR, utils.DOCS_DIRNAME)
		try:
			names = sorted(n for n in os.listdir(docs_dir)
			               if n.lower().endswith(utils.DOCUMENT_EXTENSIONS))
		except OSError:
			names = []
		if not names:
			return Response("No documentation found.", status=404, mimetype='text/plain')
		return send_from_directory(docs_dir, names[0])

	@app.route('/uploadShow', methods=['POST'])
	def upload_show() -> Response:
		"""Receive an uploaded show set as multipart form data. Files go over
		HTTP rather than the socket because a show's audio runs to tens of
		megabytes."""
		handler = app.config.get('UPLOAD_HANDLER')
		if handler is None:
			return jsonify({'success': False, 'message': 'Uploads are not available.'}), 503

		uploads = request.files.getlist('files')
		if not uploads:
			return jsonify({'success': False, 'message': 'No files were uploaded.'}), 400

		try:
			result = handler([(f.filename, f.read()) for f in uploads])
		except Exception as e:
			log.exception(f"Upload error: {e}")
			return jsonify({'success': False, 'message': str(e)}), 500

		return jsonify(result), 200 if result.get('success') else 400

	@app.route('/peer/info', methods=['GET'])
	def peer_info() -> Response:
		"""This character's identity and the notes/channels it owns."""
		handler = app.config.get('PEER_INFO_HANDLER')
		if handler is None:
			return jsonify({'error': 'Peer networking is not available.'}), 503
		return jsonify(handler())

	@app.route('/peer/show', methods=['POST'])
	def peer_show() -> Response:
		"""A host pushing a group show. The body is gzipped JSON."""
		handler = app.config.get('PEER_PUSH_HANDLER')
		if handler is None:
			return jsonify({'accepted': False, 'reason': 'unavailable'}), 503
		try:
			body = request.get_data()
			if request.headers.get('Content-Encoding', '').lower() == 'gzip':
				body = gzip.decompress(body)
			payload = json.loads(body.decode('utf-8'))
		except Exception as e:
			log.warning(f"Peer show: unreadable push from {request.remote_addr}: {e}")
			return jsonify({'accepted': False, 'reason': 'unreadable'}), 400
		try:
			result = handler(payload, request.remote_addr)
		except Exception as e:
			log.exception(f"Peer show: handler error: {e}")
			return jsonify({'accepted': False, 'reason': 'error'}), 500
		return jsonify(result), 200

	@app.route('/peer/speech', methods=['POST'])
	def peer_speech() -> Response:
		"""Another character's LLM response, or word that it finished
		speaking a line of one."""
		handler = app.config.get('PEER_SPEECH_HANDLER')
		if handler is None:
			return jsonify({'ok': False, 'reason': 'unavailable'}), 503
		payload = request.get_json(silent=True)
		if not isinstance(payload, dict):
			return jsonify({'ok': False, 'reason': 'unreadable'}), 400
		try:
			return jsonify(handler(payload, request.remote_addr)), 200
		except Exception as e:
			log.exception(f"Peer speech: handler error: {e}")
			return jsonify({'ok': False, 'reason': 'error'}), 500

	@app.route('/peer/session', methods=['GET'])
	def peer_session() -> Response:
		"""The show this character is hosting right now, for a peer that
		heard a heartbeat but missed the push."""
		handler = app.config.get('PEER_SESSION_HANDLER')
		payload = handler() if handler is not None else None
		if payload is None:
			return jsonify({'error': 'Not hosting a show.'}), 404
		body = gzip.compress(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
		return Response(body, status=200, mimetype='application/json',
			headers={'Content-Encoding': 'gzip'})

	@socketio.on('onConnect')
	def connect_event(msg: Any) -> None:
		ip = request.remote_addr
		dispatcher.send(signal='connectEvent', client_ip=ip)

	@socketio.on('showPlay')
	def show_play_event(show_name: str) -> None:
		dispatcher.send(signal='showStatus', status='play', show_name=show_name)

	@socketio.on('showStop')
	def show_stop_event() -> None:
		dispatcher.send(signal='showStatus', status='stop')

	@socketio.on('showPause')
	def show_pause_event() -> None:
		dispatcher.send(signal='showStatus', status='pause')

	@socketio.on('onMirroredMode')
	def mirrored_mode_event(bEnable: bool) -> None:
		dispatcher.send(signal='onMirroredMode', val=bEnable)

	@socketio.on('onKeyPress')
	def web_key_event(data: dict) -> None:
		dispatcher.send(signal="keyEvent", key=data["keyVal"], val=int(data["val"]))

	@socketio.on('onConnectToWifi')
	def connect_to_wifi(data: dict) -> None:
		dispatcher.send(signal="connectToWifi", ssid=data["ssid"], password=data["password"])

	@socketio.on('onWebTextSubmit')
	def web_text_submit(data: Any) -> None:
		"""Typed text from the web UI. As a command it takes the same path a
		spoken utterance would; otherwise it is spoken back verbatim."""
		if isinstance(data, dict):
			text = str(data.get("text", "")).strip()
			is_command = bool(data.get("isCommand", False))
		else:
			text, is_command = str(data).strip(), False

		if not text:
			return
		if is_command:
			dispatcher.send(signal="transcriptionResult", text=text)
		else:
			dispatcher.send(signal="executeTTS", text=text)

	@socketio.on('onConfigSave')
	def config_save_event(updates: dict) -> None:
		"""Receive config edits from the web UI as {section: {key: value}}."""
		dispatcher.send(signal="configSave", updates=updates)

	@socketio.on('onConvertShow')
	def convert_show_event(show_name: str) -> None:
		"""Convert an uploaded MIDI show into a ProgramBlue show."""
		dispatcher.send(signal="convertShow", show_name=show_name)

	@socketio.on('onRestoreBackup')
	def restore_backup_event() -> None:
		"""Copy the local backup onto an attached USB drive."""
		dispatcher.send(signal="restoreBackup")

	def __init__(self, html_config: dict = None) -> None:
		# Apply character-specific HTML settings from the hardware JSON.
		# css_file in the JSON is a project-relative path (e.g. "webpage/assets/css/kermit.css"),
		# but the browser needs it relative to the web root, so strip the "webpage/" prefix.
		if html_config:
			title = html_config.get('html_title', '').strip()
			if title:
				app.config['HTML_TITLE'] = title
			css_file = html_config.get('css_file', '').strip()
			if css_file:
				prefix = app.static_folder.rsplit(os.sep, 1)[-1] + '/'
				if css_file.startswith(prefix):
					css_file = css_file[len(prefix):]
				app.config['CSS_FILE'] = css_file
			log.info(f"WebServer: html_title='{app.config['HTML_TITLE']}', css_file='{app.config['CSS_FILE']}'")

		# Create a thread for HTTP server only
		self.threads: list[threading.Thread] = []
		http_thread = threading.Thread(target=self.run_http, daemon=True)
		self.threads.append(http_thread)
		http_thread.start()

	def set_upload_handler(self, handler) -> None:
		"""Register the callable that writes an uploaded show to storage. It
		is called on the HTTP thread and returns the result dict verbatim."""
		app.config['UPLOAD_HANDLER'] = handler

	def set_peer_handlers(self, info_handler, push_handler, session_handler, speech_handler) -> None:
		"""Register the peer callables (group shows and speech). All run on
		HTTP threads."""
		app.config['PEER_INFO_HANDLER'] = info_handler
		app.config['PEER_PUSH_HANDLER'] = push_handler
		app.config['PEER_SESSION_HANDLER'] = session_handler
		app.config['PEER_SPEECH_HANDLER'] = speech_handler

	def run_http(self) -> None:
		try:
			log.info("Starting HTTP server on port 80...")
			# flask-socketio refuses to start Werkzeug without this flag from
			# 5.3.0 on. The warning is about internet-facing deployments; this
			# server is a LAN appliance control panel, so the flag is the
			# right answer rather than dragging in eventlet or gunicorn.
			try:
				socketio.run(app, host='0.0.0.0', port=80, allow_unsafe_werkzeug=True)
			except TypeError:
				# Older flask-socketio: no such argument, and no such refusal.
				socketio.run(app, host='0.0.0.0', port=80)
		except Exception as e:
			log.exception(f"Error running HTTP server: {e}")

	def shutdown(self) -> None:
		log.info("Shutting down server...")
		# Implement shutdown logic if needed.


if __name__ == "__main__":
	import time
	server = WebServer()
	try:
		while True:
			time.sleep(0.01)
	except KeyboardInterrupt:
		server.shutdown()
