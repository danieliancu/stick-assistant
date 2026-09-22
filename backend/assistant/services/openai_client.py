"""
Thin wrapper around the official OpenAI SDK (Responses API).

- ``store=False``: OpenAI keeps no conversation state; the app sends the
  bounded history itself on every request.
- For reasoning models, encrypted reasoning items are requested
  (``include=["reasoning.encrypted_content"]``) so they can be replayed in the
  follow-up request of a tool-calling loop without server-side storage.
- SDK exceptions are mapped to two app-level errors whose messages never
  contain secrets.
"""

from __future__ import annotations

import logging
from typing import Any

import openai
from django.conf import settings

logger = logging.getLogger("assistant.openai")

REASONING_DISABLED_VALUES = {"", "off", "disabled"}


class AssistantError(Exception):
    """Base class for errors that should be shown to the user in a friendly way."""


class AssistantConfigError(AssistantError):
    """Missing key, invalid key or unsupported model."""


class AssistantUnavailableError(AssistantError):
    """Temporary OpenAI failure (network, rate limit, 5xx)."""


def is_reasoning_model(model: str) -> bool:
    name = model.lower()
    if name.startswith(("gpt-4", "gpt-3.5")) or "chat" in name:
        return False
    return name.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))


class OpenAIResponsesClient:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 reasoning_effort: str | None = None, timeout: float | None = None):
        self.api_key = api_key if api_key is not None else settings.OPENAI_API_KEY
        self.model = model or settings.OPENAI_MODEL
        self.reasoning_effort = (
            reasoning_effort if reasoning_effort is not None else settings.OPENAI_REASONING_EFFORT
        ).strip().lower()
        self.timeout = timeout or settings.OPENAI_TIMEOUT_SECONDS
        self._client: openai.OpenAI | None = None

    @property
    def client(self) -> openai.OpenAI:
        if self._client is None:
            self._client = build_openai_client(self.api_key, self.timeout)
        return self._client

    @property
    def uses_reasoning(self) -> bool:
        return (
            is_reasoning_model(self.model)
            and self.reasoning_effort not in REASONING_DISABLED_VALUES
        )

    def build_request(self, instructions: str, input_items: list, tools: list[dict]) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": input_items,
            "tools": tools,
            "store": False,
            "parallel_tool_calls": True,
        }
        if self.uses_reasoning:
            request["reasoning"] = {"effort": self.reasoning_effort}
            request["include"] = ["reasoning.encrypted_content"]
        return request

    def create_response(self, instructions: str, input_items: list, tools: list[dict]):
        request = self.build_request(instructions, input_items, tools)
        client = self.client
        try:
            return client.responses.create(**request)
        except openai.OpenAIError as exc:
            raise map_openai_error(exc, self.model) from exc


def build_openai_client(api_key: str, timeout: float) -> openai.OpenAI:
    if not api_key:
        raise AssistantConfigError(
            "OPENAI_API_KEY is not configured. Add it to backend/.env or the environment."
        )
    return openai.OpenAI(api_key=api_key, timeout=timeout, max_retries=2)


def map_openai_error(exc: openai.OpenAIError, model: str) -> AssistantError:
    """Translate an SDK exception into an app error whose message never contains secrets."""
    if isinstance(exc, openai.AuthenticationError):
        logger.error("OpenAI authentication failed (status %s)", exc.status_code)
        return AssistantConfigError("The OpenAI API key was rejected.")
    if isinstance(exc, openai.PermissionDeniedError):
        logger.error("OpenAI permission denied for model %s", model)
        return AssistantConfigError(f"The API key has no access to model '{model}'.")
    if isinstance(exc, openai.NotFoundError):
        logger.error("OpenAI model not found: %s", model)
        return AssistantConfigError(
            f"Model '{model}' was not found. Check OPENAI_MODEL / OPENAI_TRANSCRIBE_MODEL / "
            "OPENAI_TTS_MODEL.")
    if isinstance(exc, openai.BadRequestError):
        logger.error("OpenAI rejected the request (model %s): %s", model, exc.message)
        return AssistantConfigError(
            f"OpenAI rejected the request for model '{model}'. Check OPENAI_MODEL and "
            "OPENAI_REASONING_EFFORT (see server log)."
        )
    if isinstance(exc, (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError)):
        logger.warning("OpenAI temporarily unavailable: %s", type(exc).__name__)
        return AssistantUnavailableError("The AI service is temporarily unavailable.")
    if isinstance(exc, openai.APIStatusError):
        logger.error("OpenAI API error (status %s)", exc.status_code)
        return AssistantUnavailableError("The AI service returned an error.")
    logger.error("OpenAI client error: %s", type(exc).__name__)
    return AssistantUnavailableError("The AI service could not be reached.")
