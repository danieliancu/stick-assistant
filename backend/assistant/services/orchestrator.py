"""
Conversational orchestrator: user message -> OpenAI (function calling) ->
validated agenda operations -> natural-language reply.

Used identically by the terminal command and the HTTP API.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from assistant.models import AgendaItem

from .agenda import serialize_item
from .datetime_utils import build_calendar_context, now_local
from .openai_client import AssistantError, OpenAIResponsesClient
from .session import ConversationSession
from .tools import MUTATING_TOOLS, TOOL_DEFINITIONS, execute_tool

logger = logging.getLogger("assistant.orchestrator")

MAX_TOOL_ITERATIONS = 6
MAX_MESSAGE_LENGTH = 1000

BASE_INSTRUCTIONS = """\
You are a personal assistant that manages ONE person's agenda of tasks and appointments.
The agenda lives in a database you can only access through the provided tools.

Language and style:
- Reply in the language of the user's latest message (Romanian or English).
- Be brief and natural: one or two short sentences, no greetings or filler.
- Say dates naturally (e.g. "mâine la 10", "vineri, 25 septembrie", "tomorrow at 10:00").

Truthfulness (critical):
- Never claim something was saved, changed, completed or deleted unless the tool result
  for that exact operation has "ok": true in this conversation.
- If a tool returns "ok": false, tell the user it did not happen and why, briefly.
- Always call list_items to answer questions about the agenda; never answer from memory.
  If it returns no items, say the agenda is empty for that period.

Dates and times:
- Use the calendar below to turn relative expressions into YYYY-MM-DD and HH:MM (24h).
- "vineri"/"Friday" = the next Friday after today. "vinerea viitoare"/"next Friday" and
  "lunea viitoare"/"next Monday" = that weekday in next week (Monday-Sunday).
  "săptămâna viitoare"/"next week" = next Monday to Sunday.
- "la 10" / "at 10" means 10:00. Use British conventions (dd/mm).
- A date without a year means the next occurrence that is not in the past.
- If the user names today's weekday (e.g. "vineri" on a Friday) or the date/time is
  otherwise ambiguous, ask a short clarifying question instead of guessing.

Creating items:
- An appointment ("programare", "întâlnire", "appointment", "meeting") needs a date AND a
  time. If the time is missing, do NOT call create_item: ask for it (e.g. "La ce oră este
  programarea?"). Never invent a time.
- Reminders and to-dos are TASKs; a task may have no date, or a date without a time.
- Titles are short and in the user's language, e.g. "Sună la dentist".

Referring to existing items:
- Tools need the item's UUID. "îl", "-l", "o", "taskul", "it", "that" usually refer to the
  most recently referenced item listed below. Use its id; never invent ids.
- If the reference is unclear or several items could match, use get_item/list_items and ask
  the user which one. Never modify items the user did not ask about.
- Moving an item "la ora 12" keeps its date; moving it "pe vineri" keeps its time.

Deleting:
- First call delete_item with confirmed=false and ask the user to confirm. Only when the
  user's next message clearly confirms, call delete_item with confirmed=true.
"""


@dataclass
class AssistantReply:
    text: str
    status: str = "success"  # "success" | "error"
    actions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "success"


_RO_HINT = re.compile(
    r"[ăâîșşțţ]|\b(ce|am|mâine|maine|azi|astăzi|astazi|să|sa|la|pe|și|si|programare|"
    r"mută|muta|șterge|sterge|marchează|marcheaza|da|nu|ora|vineri|luni)\b",
    re.IGNORECASE,
)

_MESSAGES = {
    "ro": {
        "empty": "Nu am primit niciun mesaj.",
        "too_long": "Mesajul este prea lung.",
        "limit": "Nu am putut finaliza cererea. Te rog reformulează.",
        "no_text": "Nu am putut genera un răspuns.",
        "error": "Asistentul nu este disponibil momentan: {detail}",
        "partial": "Operațiile au fost executate ({done}), dar nu am putut genera răspunsul.",
    },
    "en": {
        "empty": "I didn't receive a message.",
        "too_long": "The message is too long.",
        "limit": "I couldn't complete the request. Please rephrase it.",
        "no_text": "I couldn't generate a reply.",
        "error": "The assistant is currently unavailable: {detail}",
        "partial": "The operations were carried out ({done}), but I couldn't generate a reply.",
    },
}


def detect_language(text: str) -> str:
    return "ro" if _RO_HINT.search(text or "") else "en"


def _to_input_item(item: Any) -> dict | None:
    """Convert a Responses API output item into an input item for replay (store=False)."""
    item_type = getattr(item, "type", None)
    if item_type == "reasoning":
        converted = {
            "type": "reasoning",
            "id": item.id,
            "summary": [
                {"type": "summary_text", "text": s.text} for s in (getattr(item, "summary", None) or [])
            ],
        }
        if getattr(item, "encrypted_content", None):
            converted["encrypted_content"] = item.encrypted_content
        return converted
    if item_type == "function_call":
        converted = {
            "type": "function_call",
            "call_id": item.call_id,
            "name": item.name,
            "arguments": item.arguments,
        }
        if getattr(item, "id", None):
            converted["id"] = item.id
        return converted
    if item_type == "message":
        return {
            "type": "message",
            "id": item.id,
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": c.text, "annotations": []}
                for c in item.content if getattr(c, "type", None) == "output_text"
            ],
        }
    return None


def _output_text(response: Any) -> str:
    parts = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) == "message":
            parts.extend(
                c.text for c in item.content if getattr(c, "type", None) == "output_text"
            )
    return "".join(parts).strip()


def _compact_for_history(items: list[dict]) -> list[dict]:
    """Drop reasoning items and item ids so stored history stays small and valid."""
    compact = []
    for item in items:
        if item.get("type") == "reasoning":
            continue
        if item.get("type") == "message" and item.get("role") == "assistant":
            text = "".join(c.get("text", "") for c in item.get("content", []))
            if text:
                compact.append({"role": "assistant", "content": text})
            continue
        if item.get("type") == "function_call":
            item = {k: v for k, v in item.items() if k != "id"}
        compact.append(item)
    return compact


class Orchestrator:
    def __init__(self, client: OpenAIResponsesClient | None = None, clock=None,
                 max_iterations: int = MAX_TOOL_ITERATIONS):
        self.client = client or OpenAIResponsesClient()
        self.clock = clock
        self.max_iterations = max_iterations

    def build_instructions(self, session: ConversationSession) -> str:
        now = now_local(self.clock)
        parts = [BASE_INSTRUCTIONS, "Calendar:", build_calendar_context(now)]
        recent = self._recent_items(session)
        if recent:
            parts.append("Recently referenced items (most recent first):")
            parts.extend(
                f"- id={i['id']} | {i['type']} | {i['title']} | {i['when'] or 'no date'} | {i['status']}"
                for i in recent
            )
        else:
            parts.append("Recently referenced items: none.")
        return "\n".join(parts)

    @staticmethod
    def _recent_items(session: ConversationSession) -> list[dict]:
        if not session.recent_items:
            return []
        by_id = {
            str(item.id): item for item in AgendaItem.objects.filter(pk__in=session.recent_items)
        }
        # Drop ids of items that no longer exist.
        session.recent_items = [i for i in session.recent_items if i in by_id]
        return [serialize_item(by_id[i]) for i in session.recent_items]

    def handle_message(self, session: ConversationSession, message: str) -> AssistantReply:
        text = (message or "").strip()
        lang = detect_language(text)
        msgs = _MESSAGES[lang]
        if not text:
            return AssistantReply(msgs["empty"], status="error")
        if len(text) > MAX_MESSAGE_LENGTH:
            return AssistantReply(msgs["too_long"], status="error")

        session.start_turn()
        instructions = self.build_instructions(session)
        turn_items: list[dict] = [{"role": "user", "content": text}]
        actions: list[dict[str, Any]] = []

        try:
            for _ in range(self.max_iterations):
                response = self.client.create_response(
                    instructions, session.history_items() + turn_items, TOOL_DEFINITIONS
                )
                output = getattr(response, "output", None) or []
                calls = [item for item in output if getattr(item, "type", None) == "function_call"]
                if not calls:
                    reply_text = _output_text(response) or msgs["no_text"]
                    turn_items.append({"role": "assistant", "content": reply_text})
                    session.commit_turn(_compact_for_history(turn_items))
                    return AssistantReply(reply_text, status="success", actions=actions)

                for item in output:
                    converted = _to_input_item(item)
                    if converted is not None:
                        turn_items.append(converted)
                for call in calls:
                    result = execute_tool(call.name, call.arguments, session, clock=self.clock)
                    actions.append({
                        "tool": call.name,
                        "ok": bool(result.get("ok")),
                        "error": result.get("error"),
                    })
                    logger.info("Tool %s -> ok=%s error=%s", call.name, result.get("ok"),
                                result.get("error"))
                    turn_items.append({
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(result, ensure_ascii=False),
                    })
        except AssistantError as exc:
            return self._failure(session, turn_items, actions, msgs,
                                 msgs["error"].format(detail=str(exc)))

        logger.warning("Tool iteration limit (%s) reached", self.max_iterations)
        return self._failure(session, turn_items, actions, msgs, msgs["limit"])

    @staticmethod
    def _failure(session, turn_items, actions, msgs, text) -> AssistantReply:
        done = [a["tool"] for a in actions if a["ok"] and a["tool"] in MUTATING_TOOLS]
        if done:
            # Be honest that the database did change even though the reply failed.
            text = f"{text} {msgs['partial'].format(done=', '.join(done))}"
            turn_items.append({"role": "assistant", "content": text})
            session.commit_turn(_compact_for_history(turn_items))
        return AssistantReply(text, status="error", actions=actions)
