#!/usr/bin/env python3
import configparser
from pydispatch import dispatcher

class LLM:
    def __init__(self) -> None:
            print("Set up LLM")

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def apply_config(self, path):
        config = configparser.ConfigParser()
        try:
            config.read(path)
        except configparser.Error as e:
            print(f"LLM: failed to parse config at '{path}': {e}")
            return

        # Set a member variable for LLMContext, OpenAIKey, DeepSeekAPIKey, and DeepSeekModel

    def send(self,query):
        # If OpenAIKey has a string, connect to OpenAI with the query and the LLMContext. If it fails, try DeepSeek instead
        # If trying DeepSeek, connect with LLMContext, DeepSeekAPIKey, DeepSeekModel. If it fails, cotninue.
        # if all failed, print an error that LLM isn't supported.
        # If we connected and got a response, call _on_response with the response
        print("TODO")

    # -------------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------------

    def _on_response(self, response):
        dispatcher.send(signal="llmResponse", response=response)
        print("RESPONSE")