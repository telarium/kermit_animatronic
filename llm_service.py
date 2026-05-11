#!/usr/bin/env python3
import configparser
import threading
from collections import deque
import anthropic
from openai import OpenAI
from pydispatch import dispatcher


class LLM:
	HISTORY_LIMIT = 20  # number of exchanges (user + assistant pairs) to remember

	def __init__(self) -> None:
		self.llm_context: str = ""
		self.anthropic_key: str = ""
		self.anthropic_model: str = "claude-sonnet-4-6"
		self.openai_key: str = ""
		self.deepseek_api_key: str = ""
		self.deepseek_model: str = "deepseek-chat"

		# Conversation history as a deque of {"role": ..., "content": ...} dicts.
		# Capped at HISTORY_LIMIT * 2 messages (user + assistant per exchange).
		self._history: deque = deque(maxlen=self.HISTORY_LIMIT * 2)

	# -------------------------------------------------------------------------
	# Public API
	# -------------------------------------------------------------------------

	def apply_config(self, path: str) -> None:
		config = configparser.ConfigParser()
		try:
			config.read(path)
		except configparser.Error as e:
			print(f"LLM: failed to parse config at '{path}': {e}")
			return

		self.anthropic_key    = config.get("Anthropic", "AnthropicKey",   fallback="").strip()
		self.anthropic_model  = config.get("Anthropic", "AnthropicModel", fallback="claude-sonnet-4-6").strip()
		self.openai_key       = config.get("ChatGPT",   "OpenAIKey",      fallback="").strip()
		self.deepseek_api_key = config.get("DeepSeek",  "DeepSeekAPIKey", fallback="").strip()
		self.deepseek_model   = config.get("DeepSeek",  "DeepSeekModel",  fallback="deepseek-chat").strip()
		self.llm_context      = config.get("DeepSeek",  "LLMContext",     fallback="").strip().strip('"')

	def send(self, query: str) -> None:
		"""Send a query to the LLM asynchronously so we never block the main thread."""
		threading.Thread(target=self._send, args=(query,), daemon=True).start()

	def clear_history(self) -> None:
		"""Reset conversation history — call this to start a fresh conversation."""
		self._history.clear()
		print("LLM: conversation history cleared.")

	# -------------------------------------------------------------------------
	# Internal
	# -------------------------------------------------------------------------

	def _on_fail(self):
		dispatcher.send(signal="playVoiceFile", file="no_ai.ogg")

	def _build_messages(self, query: str) -> list:
		"""Build the messages list from history + current query."""
		messages = list(self._history)
		messages.append({"role": "user", "content": query})
		return messages

	def _send(self, query: str) -> None:
		response = None

		dispatcher.send(signal="updateStatus", id="A.I. Responding To", value=str)

		messages = self._build_messages(query)

		# Try Anthropic (Claude) first
		if self.anthropic_key:
			try:
				client = anthropic.Anthropic(api_key=self.anthropic_key)
				result = client.messages.create(
					model=self.anthropic_model,
					max_tokens=1024,
					system=self.llm_context,
					messages=messages,
				)
				response = result.content[0].text
			except Exception as e:
				print(f"LLM: Anthropic request failed: {e}")

		# Fall back to OpenAI
		if response is None and self.openai_key:
			try:
				full_messages = []
				if self.llm_context:
					full_messages.append({"role": "system", "content": self.llm_context})
				full_messages.extend(messages)
				client = OpenAI(api_key=self.openai_key)
				result = client.chat.completions.create(
					model="gpt-4o-mini",
					messages=full_messages,
				)
				response = result.choices[0].message.content
			except Exception as e:
				print(f"LLM: OpenAI request failed: {e}")

		# Fall back to DeepSeek
		if response is None and self.deepseek_api_key:
			try:
				full_messages = []
				if self.llm_context:
					full_messages.append({"role": "system", "content": self.llm_context})
				full_messages.extend(messages)
				client = OpenAI(
					api_key=self.deepseek_api_key,
					base_url="https://api.deepseek.com",
				)
				result = client.chat.completions.create(
					model=self.deepseek_model,
					messages=full_messages,
				)
				response = result.choices[0].message.content
			except Exception as e:
				print(f"LLM: DeepSeek request failed: {e}")

		if response is None:
			self._on_fail()
			print("LLM: all providers failed — no response available.")
			return

		# Store the exchange in history
		self._history.append({"role": "user",      "content": query})
		self._history.append({"role": "assistant",  "content": response})

		self._on_response(response)

	def _on_response(self, response: str) -> None:
		dispatcher.send(signal="executeTTS", text=response)
		dispatcher.send(signal="updateStatus", id="A.I. Responding", value=response)