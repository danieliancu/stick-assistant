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
        if not self.api_key:
            raise AssistantConfigError(
                "OPENAI_API_KEY is not configured. Add it to backend/.env or the environment."
            )
        if self._client is None:
            self._client = openai.OpenAI(api_key=self.api_key, timeout=self.timeout, max_retries=2)
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
        except openai.AuthenticationError as exc:
            logger.error("OpenAI authentication failed (status %s)", exc.status_code)
            raise AssistantConfigError("The OpenAI API key was rejected.") from exc
        except openai.PermissionDeniedError as exc:
            logger.error("OpenAI permission denied for model %s", self.model)
            raise AssistantConfigError(
                f"The API key has no access to model '{self.model}'."
            ) from exc
        except openai.NotFoundError as exc:
            logger.error("OpenAI model not found: %s", self.model)
            raise AssistantConfigError(
                f"Model '{self.model}' was not found. Check OPENAI_MODEL."
            ) from exc
        except openai.BadRequestError as exc:
            logger.error("OpenAI rejected the request (model %s): %s", self.model, exc.message)
            raise AssistantConfigError(
                f"OpenAI rejected the request. Check that model '{self.model}' supports the "
                "Responses API and function calling."
            ) from exc
        except (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError) as exc:
            logger.warning("OpenAI temporarily unavailable: %s", type(exc).__name__)
            raise AssistantUnavailableError("The AI service is temporarily unavailable.") from exc
        except openai.APIStatusError as exc:
            logger.error("OpenAI API error (status %s)", exc.status_code)
            raise AssistantUnavailableError("The AI service returned an error.") from exc
        except openai.OpenAIError as exc:
            logger.error("OpenAI client error: %s", type(exc).__name__)
            raise AssistantUnavailableError("The AI service could not be reached.") from exc
