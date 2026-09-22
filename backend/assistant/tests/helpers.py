"""Test doubles for the OpenAI Responses API. No network calls are made."""

import copy
import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace

# Tuesday 22 September 2026, 10:00 BST (09:00 UTC).
FIXED_NOW = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)


def fixed_clock(now: datetime = FIXED_NOW):
    return lambda: now


def function_call(name: str, args: dict | str, call_id: str = "call_1"):
    arguments = args if isinstance(args, str) else json.dumps(args)
    return SimpleNamespace(type="function_call", id=f"fc_{call_id}", call_id=call_id,
                           name=name, arguments=arguments)


def message(text: str):
    return SimpleNamespace(type="message", id="msg_1", role="assistant",
                           content=[SimpleNamespace(type="output_text", text=text)])


def reasoning(encrypted: str = "encrypted-blob"):
    return SimpleNamespace(type="reasoning", id="rs_1", summary=[], encrypted_content=encrypted)


def response(*items):
    return SimpleNamespace(output=list(items))


def tool_outputs(input_items: list) -> list[dict]:
    """Parsed JSON results of all function_call_output items sent to the model."""
    return [json.loads(i["output"]) for i in input_items
            if isinstance(i, dict) and i.get("type") == "function_call_output"]


def recent_ids(instructions: str) -> list[str]:
    return re.findall(r"id=([0-9a-f-]{36})", instructions)


class FakeResponsesClient:
    """
    Scripted stand-in for OpenAIResponsesClient.

    Each scripted step is either a response object or a callable
    ``(instructions, input_items) -> response`` so a test can react to the
    real tool results / context exactly like the model would.
    """

    def __init__(self, steps=None):
        self.steps = list(steps or [])
        self.calls: list[dict] = []

    def add(self, *steps):
        self.steps.extend(steps)

    def create_response(self, instructions, input_items, tools):
        self.calls.append({
            "instructions": instructions,
            "input": copy.deepcopy(input_items),
            "tools": tools,
        })
        if not self.steps:
            raise AssertionError("FakeResponsesClient ran out of scripted responses")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(instructions, input_items) if callable(step) else step
