#!/usr/bin/env python3
import configparser
import re
import threading
import time
from collections import deque
from typing import List, Optional
import anthropic
import requests
from openai import OpenAI
from pydispatch import dispatcher


class LLM:
	HISTORY_LIMIT = 20  # number of exchanges (user + assistant pairs) to remember

	# Offline history is tuned independently of the cloud one — a 1.7B loses
	# the thread long before 20 exchanges.
	FALLBACK_HISTORY_TURNS = 3

	CLOUD_TIMEOUT_S = 8.0
	MIN_ATTEMPT_S = 0.5  # below this there isn't time for a useful attempt

	FALLBACK_URL = "http://127.0.0.1:8081"
	FALLBACK_HEALTH_TIMEOUT_S = 2.0
	FALLBACK_TIMEOUT_S = 20.0
	# The warm-up is the request that pays the cold prompt-eval cost, so it
	# cannot share the runtime timeout.
	FALLBACK_WARMUP_TIMEOUT_S = 180.0
	FALLBACK_MAX_TOKENS = 60

	# Prepended to every offline request and never subject to history
	# trimming. Without it the model breaks first-person binding and starts
	# addressing the character instead of being him.
	# Prepended to every offline request and never subject to history
	# trimming. Without them the model breaks first-person binding and starts
	# addressing the character instead of being it.
	#
	# The exchange has to stay coherent; a non-sequitur here teaches the model
	# that non-sequiturs are acceptable.
	FALLBACK_EXAMPLES = [
		{"role": "user", "content": "Hi there!"},
		{"role": "assistant", "content": "Well, hi! Gee, it's good to see you!"},
		{"role": "user", "content": "Ask me a question."},
		{"role": "assistant", "content": "Oh, sure! Let me think... where do you know me from? [?]"},
		# Negative case. With only the pair above, the model read [?] as "how
		# replies end" rather than "how questions end" and appended it to
		# plain statements too.
		{"role": "user", "content": "From the show!"},
		{"role": "assistant", "content": "That's right! You've got a good memory."},
	]

	# The offline counterpart to CONTEXT_POSTFIX, kept to two rules. It lives
	# in code rather than LLMContextShort so the config stays purely character
	# voice, and so [?] can't be edited away by accident.
	FALLBACK_POSTFIX = (
		" Stay in first person as the character described above."
		" Never greet or address that character as though they were someone else."
		" If you ask the user a question you need an answer to, end your reply"
		" with exactly [?]."
	)

	CONTEXT_POSTFIX = (
		" Only use spoken words in your responses and not actions."
		" If you are asking the user a direct question that requires their response,"
		" end your message with the appropriate punctuation and then exactly [?]."
		" Only use [?] when you genuinely need their answer to continue, but not for rhetorical questions."
		" The user's words reach you through speech recognition and may contain phonetic errors,"
		" where a word is replaced by something that sounds similar"
		" (for example 'started' in place of 'starred', or 'wear' in place of 'where')."
		" If a sentence seems odd, ungrammatical, or slightly nonsensical, re-read the questionable"
		" word by sound and respond to what the user most likely meant."
		" Never mention the transcription, correct their wording, or ask whether they meant a"
		" different word... simply answer the intended meaning."
		" Names may be phonetically transcribed. Partial answers that capture the key fact should be accepted."
		" Always assume the user meant the correct answer if there is any reasonable interpretation that matches."
		" When in doubt, accept the answer and move on enthusiastically."
		" If the user's message is [SILENCE], respond as if they didn't answer —"
		" gently prompt them again or move on naturally."
		" Write plain spoken text only. Use letters, numbers, and ordinary sentence"
		" punctuation — periods, commas, question marks, exclamation points, apostrophes,"
		" and hyphens. Apart from the [?] marker described above, never use asterisks,"
		" ampersands, brackets, quotation marks, emoji, markdown, or any other symbol."
		" Spell symbols out as words instead: 'and' rather than '&', 'percent' rather than '%'."
	)

	def __init__(self) -> None:
		self.llm_context: str = ""
		self.llm_context_short: str = ""
		self.anthropic_key: str = ""
		self.anthropic_model: str = "claude-sonnet-4-6"
		self.openai_key: str = ""
		self.deepseek_api_key: str = ""
		self.deepseek_model: str = "deepseek-chat"

		# Conversation history as a deque of {"role": ..., "content": ...} dicts.
		# Capped at HISTORY_LIMIT * 2 messages (user + assistant per exchange).
		self._history: deque = deque(maxlen=self.HISTORY_LIMIT * 2)
		# Kept separate so cloud replies never seed the local model, and the
		# local model's confabulations never become Claude's prior turns.
		self._fallback_history: deque = deque(maxlen=self.FALLBACK_HISTORY_TURNS * 2)

		self._fallback_available: bool = False
		self._last_tier: str = ""
		self._warmed: bool = False

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
		self.llm_context      = config.get("LLM",       "LLMContext",     fallback="").strip()
		if not self.llm_context:
			# Backward compat: LLMContext used to live under [DeepSeek].
			self.llm_context = config.get("DeepSeek", "LLMContext", fallback="").strip()

		self.llm_context_short = config.get("LLM", "LLMContextShort", fallback="").strip()
		if not self.llm_context_short:
			print("LLM: no LLMContextShort set, offline path will use the full context.")
			self.llm_context_short = self.llm_context

		self.warm_up()

	def send(self, query: str) -> None:
		"""Send a query to the LLM asynchronously so we never block the main thread."""
		threading.Thread(target=self._send, args=(query,), daemon=True).start()

	def clear_history(self) -> None:
		"""Reset conversation history — call this to start a fresh conversation."""
		self._history.clear()
		self._fallback_history.clear()
		self._last_tier = ""
		print("LLM: conversation history cleared.")

	def warm_up(self) -> None:
		"""Health-check the local server and pay its cold prompt-eval cost once,
		off the main thread. Never starts the server — that's systemd's job."""
		if self._warmed:
			return
		self._warmed = True
		threading.Thread(target=self._warm_up, daemon=True).start()

	# -------------------------------------------------------------------------
	# Internal
	# -------------------------------------------------------------------------

	def _on_fail(self) -> None:
		dispatcher.send(signal="playVoiceFile", file="no_ai.ogg")

	def _build_messages(self, user_text: str, history, fallback: bool = False) -> list:
		"""Build the messages list. The system prompt is always element 0;
		callers targeting Anthropic split it back out."""
		if fallback:
			messages = [{"role": "system",
				"content": self.llm_context_short + self.FALLBACK_POSTFIX}]
			messages += self.FALLBACK_EXAMPLES
			turns = self.FALLBACK_HISTORY_TURNS
		else:
			messages = [{"role": "system",
				"content": self._cloud_context() + self.CONTEXT_POSTFIX}]
			turns = self.HISTORY_LIMIT
		messages += list(history)[-turns * 2:]
		messages.append({"role": "user", "content": user_text})
		return messages

	def _cloud_context(self) -> str:
		"""LLMContextShort carries identity, LLMContext carries personality —
		the cloud models get both. The offline path takes the short one alone,
		which is the whole reason the two are separate fields."""
		parts = [p for p in (self.llm_context_short, self.llm_context) if p]
		return " ".join(parts)

	def _send(self, query: str) -> None:
		dispatcher.send(signal="updateStatus", id="A.I. Responding To", value=query)

		t0 = time.monotonic()
		response = self._send_cloud(query)
		if response is not None:
			print(f"LLM: cloud replied in {time.monotonic() - t0:.1f}s.")
			self._last_tier = "cloud"
			self._history.append({"role": "user",      "content": query})
			self._history.append({"role": "assistant", "content": response})
			self._on_response(response)
			return

		t1 = time.monotonic()
		response = self._send_fallback(query)
		if response is not None:
			print(f"LLM: cloud gave up after {t1 - t0:.1f}s; "
			      f"offline replied in {time.monotonic() - t1:.1f}s.")
			self._on_response(response)
			return

		print("LLM: all providers failed — no response available.")
		self._on_fail()

	def _send_cloud(self, query: str) -> Optional[str]:
		messages = self._build_messages(query, self._history)
		system_prompt = messages[0]["content"]
		body = messages[1:]
		deadline = time.monotonic() + self.CLOUD_TIMEOUT_S

		def remaining() -> float:
			return deadline - time.monotonic()

		if self.anthropic_key and remaining() > self.MIN_ATTEMPT_S:
			try:
				client = anthropic.Anthropic(
					api_key=self.anthropic_key, timeout=remaining(), max_retries=0
				)
				result = client.messages.create(
					model=self.anthropic_model,
					max_tokens=1024,
					system=system_prompt,
					messages=body,
				)
				return result.content[0].text
			except Exception as e:
				print(f"LLM: Anthropic request failed: {e}")

		if self.openai_key and remaining() > self.MIN_ATTEMPT_S:
			try:
				client = OpenAI(api_key=self.openai_key, timeout=remaining(), max_retries=0)
				result = client.chat.completions.create(
					model="gpt-4o-mini",
					messages=messages,
				)
				return result.choices[0].message.content
			except Exception as e:
				print(f"LLM: OpenAI request failed: {e}")

		if self.deepseek_api_key and remaining() > self.MIN_ATTEMPT_S:
			try:
				client = OpenAI(
					api_key=self.deepseek_api_key,
					base_url="https://api.deepseek.com",
					timeout=remaining(),
					max_retries=0,
				)
				result = client.chat.completions.create(
					model=self.deepseek_model,
					messages=messages,
				)
				return result.choices[0].message.content
			except Exception as e:
				print(f"LLM: DeepSeek request failed: {e}")

		return None

	def _send_fallback(self, query: str) -> Optional[str]:
		if not self._fallback_available and not self._health_check():
			print("LLM: offline model unavailable.")
			return None
		self._fallback_available = True

		if self._last_tier != "fallback":
			print("LLM: failing over to the offline model.")
			self._fallback_history.clear()

		messages = self._build_messages(query, self._fallback_history, fallback=True)
		text = self._request_fallback(messages, self.FALLBACK_MAX_TOKENS)
		if text is None:
			self._fallback_available = False
			return None

		text = self._strip_stray_marker(text)
		self._last_tier = "fallback"
		self._fallback_history.append({"role": "user",      "content": query})
		self._fallback_history.append({"role": "assistant", "content": text})
		return text

	_TRAILING_MARKER_RE = re.compile(r"\s*\[\s*\?\s*\]\s*[.!?,]*\s*$")

	@classmethod
	def _strip_stray_marker(cls, text: str) -> str:
		"""Drop a trailing [?] the model tacked onto a statement.
		"""
		match = cls._TRAILING_MARKER_RE.search(text)
		if not match:
			return text
		body = text[:match.start()].rstrip()
		if body.endswith("?"):
			# Re-emit it bare so trailing punctuation can't hide it downstream.
			return body + " [?]"
		print("LLM: dropped a [?] the offline model added to a statement.")
		return body

	def _health_check(self) -> bool:
		try:
			result = requests.get(
				f"{self.FALLBACK_URL}/health", timeout=self.FALLBACK_HEALTH_TIMEOUT_S
			)
			return result.status_code == 200
		except Exception:
			return False

	def _request_fallback(self, messages: List[dict], max_tokens: int,
	                      timeout: Optional[float] = None) -> Optional[str]:
		try:
			result = requests.post(
				f"{self.FALLBACK_URL}/v1/chat/completions",
				json={
					"messages": messages,
					"max_tokens": max_tokens,
					"temperature": 0.7,
					"top_p": 0.8,
					"top_k": 20,
					"presence_penalty": 1.0,
					"stop": ["*"],
					# Qwen3 is a hybrid reasoning model and will otherwise emit
					# a <think> block straight into TTS. The /no_think prompt
					# convention is unreliable; the template kwarg is not.
					# "<think>" must NOT go in "stop" — the model emits it as
					# the first token, which returns empty content.
					"chat_template_kwargs": {"enable_thinking": False},
				},
				timeout=timeout or self.FALLBACK_TIMEOUT_S,
			)
			result.raise_for_status()
			return result.json()["choices"][0]["message"]["content"].strip()
		except Exception as e:
			print(f"LLM: offline request failed: {e}")
			return None

	def _warm_up(self) -> None:
		self._fallback_available = self._health_check()
		if not self._fallback_available:
			print(f"LLM: no offline model at {self.FALLBACK_URL} — cloud only.")
			return

		t0 = time.monotonic()
		messages = self._build_messages("Hi Kermit!", [], fallback=True)
		# max_tokens=1, and the result is discarded — it must never be
		# dispatched for playback.
		if self._request_fallback(messages, 1, self.FALLBACK_WARMUP_TIMEOUT_S) is None:
			print("LLM: offline warm-up request failed.")
			self._fallback_available = False
			return
		elapsed = time.monotonic() - t0
		print(f"LLM: offline model warm in {elapsed:.1f}s.")
		if elapsed > 60:
			print("LLM: that is slow for a 1.7B — check that llama-server "
			      "offloaded its layers to the GPU (journalctl -u llama-server "
			      "| grep -i offload).")

	def _on_response(self, response: str) -> None:
		dispatcher.send(signal="executeTTS", text=response)
		dispatcher.send(signal="updateStatus", id="A.I. Responding", value=response)
