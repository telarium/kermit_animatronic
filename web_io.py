import os
import socket
import threading
import logging
from flask import Flask, request, Response
from flask_socketio import SocketIO
from pydispatch import dispatcher
from typing import Any

# Turn off extra log messages
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__, static_folder='webpage')
app.config['SECRET_KEY'] = 'Monkey Island is an amusement park.'

# Character-specific HTML values, injected into index.html at serve time.
# Overridden via the "html" section of the hardware JSON (see WebServer.__init__).
app.config['HTML_TITLE'] = 'Animatronic Controller'
app.config['CSS_FILE']   = 'assets/css/kermit.css'

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
				print(f"Broadcast error: {e}")

	@app.route('/<path:path>')
	def static_proxy(path: str) -> Response:
		return app.send_static_file(path)

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

	@socketio.on('onWebTTSSubmit')
	def web_tts_submit(inputText: str) -> None:
		dispatcher.send(signal="executeTTS", text=inputText)

	@socketio.on('onConfigSave')
	def config_save_event(updates: dict) -> None:
		"""Receive config edits from the web UI as {section: {key: value}}."""
		dispatcher.send(signal="configSave", updates=updates)

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
			print(f"WebServer: html_title='{app.config['HTML_TITLE']}', css_file='{app.config['CSS_FILE']}'")

		# Create a thread for HTTP server only
		self.threads: list[threading.Thread] = []
		http_thread = threading.Thread(target=self.run_http, daemon=True)
		self.threads.append(http_thread)
		http_thread.start()

	def run_http(self) -> None:
		try:
			print("Starting HTTP server on port 80...")
			socketio.run(app, host='0.0.0.0', port=80)
		except Exception as e:
			print(f"Error running HTTP server: {e}")

	def shutdown(self) -> None:
		print("Shutting down server...")
		# Implement shutdown logic if needed.


if __name__ == "__main__":
	import time
	server = WebServer()
	try:
		while True:
			time.sleep(0.01)
	except KeyboardInterrupt:
		server.shutdown()
