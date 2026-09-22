"""
Bounded, in-memory conversation state for one interactive session.

Agenda data lives in SQLite; this only keeps the short-term context needed for
follow-ups ("mută-l pe vineri"): recent conversation items, the UUIDs of
recently referenced agenda items and a pending delete confirmation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

MAX_TURNS = 8
MAX_RECENT_ITEMS = 10
DEVICE_SESSION_IDLE_TIMEOUT = timedelta(minutes=30)


@dataclass
class PendingDelete:
    item_id: str
    turn_no: int


@dataclass
class ConversationSession:
    max_turns: int = MAX_TURNS
    turns: list[list[dict[str, Any]]] = field(default_factory=list)
    recent_items: list[str] = field(default_factory=list)
    pending_delete: PendingDelete | None = None
    turn_no: int = 0
    last_active: datetime = field(default_factory=timezone.now)

    def start_turn(self) -> int:
        self.turn_no += 1
        self.last_active = timezone.now()
        return self.turn_no

    def history_items(self) -> list[dict[str, Any]]:
        """Previous turns as Responses API input items (whole turns only)."""
        return [item for turn in self.turns for item in turn]

    def commit_turn(self, items: list[dict[str, Any]]) -> None:
        """Store a completed turn; old turns are dropped as whole units so
        function_call / function_call_output pairs are never split."""
        self.turns.append(items)
        if len(self.turns) > self.max_turns:
            self.turns = self.turns[-self.max_turns:]

    def remember(self, *item_ids: str) -> None:
        """Mark agenda items as recently referenced (most recent first)."""
        for item_id in item_ids:
            item_id = str(item_id)
            if item_id in self.recent_items:
                self.recent_items.remove(item_id)
            self.recent_items.insert(0, item_id)
        del self.recent_items[MAX_RECENT_ITEMS:]

    def forget(self, item_id: str) -> None:
        item_id = str(item_id)
        if item_id in self.recent_items:
            self.recent_items.remove(item_id)
        if self.pending_delete and self.pending_delete.item_id == item_id:
            self.pending_delete = None

    def is_idle(self, now: datetime | None = None) -> bool:
        return (now or timezone.now()) - self.last_active > DEVICE_SESSION_IDLE_TIMEOUT


# --- Single device session (HTTP API) ------------------------------------------

DEVICE_SESSION_LOCK = threading.Lock()
_device_session: ConversationSession | None = None


def get_device_session() -> ConversationSession:
    """The one in-process session for the single device; reset after inactivity.
    Callers should hold ``DEVICE_SESSION_LOCK`` while using it."""
    global _device_session
    if _device_session is None or _device_session.is_idle():
        _device_session = ConversationSession()
    return _device_session


def reset_device_session() -> None:
    global _device_session
    _device_session = None
