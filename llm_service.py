#!/usr/bin/env python3
import configparser
import threading
import anthropic
from openai import OpenAI
from pydispatch import dispatcher


class LLM:
	def __init__(self) -> None:
		self.llm_context: str = ""
		self.anthropic_key: str = ""
		self.anthropic_model: str = "claude-sonnet-4-6"
		self.openai_key: str = ""
		self.deepseek_api_key: str = ""
		self.deepseek_model: str = "deepseek-chat"

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

	# -------------------------------------------------------------------------
	# Internal
	# -------------------------------------------------------------------------

	def _send(self, query: str) -> None:
		response = None

		dispatcher.send(signal="updateStatus", id="A.I. Responding To", value=str)

		# Try Anthropic (Claude) first
		if self.anthropic_key:
			try:
				client = anthropic.Anthropic(api_key=self.anthropic_key)
				result = client.messages.create(
					model=self.anthropic_model,
					max_tokens=1024,
					system=self.llm_context,
					messages=[{"role": "user", "content": query}],
				)
				response = result.content[0].text
			except Exception as e:
				print(f"LLM: Anthropic request failed: {e}")

		# Fall back to OpenAI
		if response is None and self.openai_key:
			try:
				messages = []
				if self.llm_context:
					messages.append({"role": "system", "content": self.llm_context})
				messages.append({"role": "user", "content": query})
				client = OpenAI(api_key=self.openai_key)
				result = client.chat.completions.create(
					model="gpt-4o-mini",
					messages=messages,
				)
				response = result.choices[0].message.content
			except Exception as e:
				print(f"LLM: OpenAI request failed: {e}")

		# Fall back to DeepSeek
		if response is None and self.deepseek_api_key:
			try:
				messages = []
				if self.llm_context:
					messages.append({"role": "system", "content": self.llm_context})
				messages.append({"role": "user", "content": query})
				client = OpenAI(
					api_key=self.deepseek_api_key,
					base_url="https://api.deepseek.com",
				)
				result = client.chat.completions.create(
					model=self.deepseek_model,
					messages=messages,
				)
				response = result.choices[0].message.content
			except Exception as e:
				print(f"LLM: DeepSeek request failed: {e}")

		if response is None:
			print("LLM: all providers failed — no response available.")
			return

		self._on_response(response)

	def _on_response(self, response: str) -> None:
		dispatcher.send(signal="executeTTS", text=response)
		dispatcher.send(signal="updateStatus", id="A.I. Responding", value=response)
