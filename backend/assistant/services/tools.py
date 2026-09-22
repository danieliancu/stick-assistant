"""
Function-calling tools exposed to the model.

Each tool has a strict JSON schema (sent to OpenAI) and a server-side handler
that re-validates every argument deterministically before calling the agenda
service. Handlers always return a JSON-serialisable dict; ``ok`` is true only
when the database operation actually succeeded.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time
from typing import Any, Callable

from django.db import DatabaseError

from assistant.models import AgendaItem

from . import agenda
from .datetime_utils import (
    DateTimeValidationError,
    make_local_aware,
    now_local,
    parse_local_date,
    parse_local_time,
    to_local,
)
from .session import ConversationSession, PendingDelete

logger = logging.getLogger("assistant.tools")

MAX_TITLE_LENGTH = 200
MAX_TEXT_LENGTH = 2000
MAX_RECENT_LISTED = 5  # listed items remembered as recent references


class ToolArgumentError(ValueError):
    """Model-supplied arguments failed server-side validation."""


# --- Schemas -------------------------------------------------------------------

_NULLABLE_STRING = {"type": ["string", "null"]}
_DATE_DESC = "Local date YYYY-MM-DD (Europe/London), or null."
_TIME_DESC = "Local 24-hour time HH:MM (Europe/London), or null."


def _tool(name: str, description: str, properties: dict) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOL_DEFINITIONS: list[dict] = [
    _tool(
        "create_item",
        "Create a TASK or APPOINTMENT. Appointments need both date and time; never guess "
        "a missing appointment time. Tasks may have no date, a date only, or date and time.",
        {
            "type": {"type": "string", "enum": ["TASK", "APPOINTMENT"]},
            "title": {"type": "string", "description": "Short title, e.g. 'Sună la dentist'."},
            "description": {**_NULLABLE_STRING, "description": "Optional details, or null."},
            "date": {**_NULLABLE_STRING, "description": _DATE_DESC},
            "time": {**_NULLABLE_STRING, "description": _TIME_DESC},
        },
    ),
    _tool(
        "list_items",
        "Query the agenda database. Use for every question about what is on the agenda. "
        "date_from/date_to are inclusive; use the same value for a single day.",
        {
            "date_from": {**_NULLABLE_STRING, "description": _DATE_DESC},
            "date_to": {**_NULLABLE_STRING, "description": _DATE_DESC},
            "type": {"type": ["string", "null"], "enum": ["TASK", "APPOINTMENT", None]},
            "status": {
                "type": "string",
                "enum": ["active", "completed", "cancelled", "all"],
                "description": "Use 'active' unless the user asks for completed/cancelled items.",
            },
            "include_undated": {
                "type": "boolean",
                "description": "Also include tasks without a date when filtering by date.",
            },
            "query": {**_NULLABLE_STRING, "description": "Text to search in title/description, or null."},
        },
    ),
    _tool(
        "get_item",
        "Get one item by id, or search by text to resolve which item the user means. "
        "If several items match, ask the user which one.",
        {
            "item_id": {**_NULLABLE_STRING, "description": "Item UUID, or null to search."},
            "query": {**_NULLABLE_STRING, "description": "Search text, or null when item_id is given."},
            "include_inactive": {
                "type": "boolean",
                "description": "Also search completed/cancelled items.",
            },
        },
    ),
    _tool(
        "update_item",
        "Update an existing item identified by its UUID. Null fields stay unchanged. "
        "Giving only a time keeps the item's current date; giving only a date keeps its time.",
        {
            "item_id": {"type": "string", "description": "UUID of the item."},
            "title": _NULLABLE_STRING,
            "description": _NULLABLE_STRING,
            "date": {**_NULLABLE_STRING, "description": _DATE_DESC},
            "time": {**_NULLABLE_STRING, "description": _TIME_DESC},
            "status": {
                "type": ["string", "null"],
                "enum": ["PENDING", "CANCELLED", None],
                "description": "Set CANCELLED to cancel, PENDING to reopen; null to keep.",
            },
        },
    ),
    _tool(
        "complete_item",
        "Mark an item as completed.",
        {"item_id": {"type": "string", "description": "UUID of the item."}},
    ),
    _tool(
        "delete_item",
        "Permanently delete an item. First call with confirmed=false and ask the user to "
        "confirm. Only after the user explicitly confirms in their next message, call again "
        "with confirmed=true.",
        {
            "item_id": {"type": "string", "description": "UUID of the item."},
            "confirmed": {"type": "boolean"},
        },
    ),
]

TOOL_NAMES = {tool["name"] for tool in TOOL_DEFINITIONS}
MUTATING_TOOLS = {"create_item", "update_item", "complete_item", "delete_item"}


# --- Argument validation helpers -------------------------------------------------

def _check_keys(args: Any, expected: set[str]) -> dict:
    if not isinstance(args, dict):
        raise ToolArgumentError("Arguments must be a JSON object.")
    missing = expected - set(args)
    extra = set(args) - expected
    if missing or extra:
        raise ToolArgumentError(
            f"Unexpected argument set (missing={sorted(missing)}, extra={sorted(extra)})."
        )
    return args


def _opt_str(args: dict, key: str, max_length: int = MAX_TEXT_LENGTH) -> str | None:
    value = args[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolArgumentError(f"'{key}' must be a string or null.")
    value = value.strip()
    if len(value) > max_length:
        raise ToolArgumentError(f"'{key}' is too long (max {max_length} characters).")
    return value or None


def _req_str(args: dict, key: str, max_length: int = MAX_TEXT_LENGTH) -> str:
    value = _opt_str(args, key, max_length)
    if not value:
        raise ToolArgumentError(f"'{key}' is required and cannot be empty.")
    return value


def _bool(args: dict, key: str) -> bool:
    value = args[key]
    if not isinstance(value, bool):
        raise ToolArgumentError(f"'{key}' must be a boolean.")
    return value


def _enum(args: dict, key: str, allowed: set, nullable: bool = False):
    value = args[key]
    if value is None and nullable:
        return None
    if value not in allowed:
        raise ToolArgumentError(f"'{key}' must be one of {sorted(a for a in allowed if a)}.")
    return value


def _opt_date(args: dict, key: str) -> date | None:
    value = _opt_str(args, key, 20)
    return parse_local_date(value) if value else None


def _opt_time(args: dict, key: str) -> time | None:
    value = _opt_str(args, key, 10)
    return parse_local_time(value) if value else None


def _is_past(value: datetime, has_time: bool, clock) -> bool:
    now = now_local(clock)
    if has_time:
        return value < now
    return to_local(value).date() < now.date()


# --- Handlers ------------------------------------------------------------------

class ToolContext:
    def __init__(self, session: ConversationSession, clock=None):
        self.session = session
        self.clock = clock


def _item_result(item: AgendaItem, **extra) -> dict:
    return {"ok": True, "item": agenda.serialize_item(item), **extra}


def _create_item(args: dict, ctx: ToolContext) -> dict:
    _check_keys(args, {"type", "title", "description", "date", "time"})
    item_type = _enum(args, "type", set(AgendaItem.Type.values))
    title = _req_str(args, "title", MAX_TITLE_LENGTH)
    description = _opt_str(args, "description") or ""
    day = _opt_date(args, "date")
    at = _opt_time(args, "time")

    starts_at = due_at = None
    due_has_time = True
    if item_type == AgendaItem.Type.APPOINTMENT:
        if day is None:
            return {"ok": False, "error": "missing_appointment_date",
                    "message": "Appointments need a date. Ask the user for the date."}
        if at is None:
            return {"ok": False, "error": "missing_appointment_time",
                    "message": "Appointments need a time. Ask the user what time it is."}
        starts_at = make_local_aware(day, at)
        when, has_time = starts_at, True
    else:
        if at is not None and day is None:
            raise ToolArgumentError("A task time requires a date. Ask the user which day.")
        if day is not None:
            due_has_time = at is not None
            due_at = make_local_aware(day, at or time(0, 0))
        when, has_time = due_at, due_has_time

    item = agenda.create_item(
        type=item_type, title=title, description=description,
        starts_at=starts_at, due_at=due_at, due_has_time=due_has_time,
    )
    ctx.session.remember(item.id)
    extra = {"created": True}
    if when is not None and _is_past(when, has_time, ctx.clock):
        extra["warning"] = "The saved date/time is in the past. Mention this to the user."
    return _item_result(item, **extra)


_STATUS_FILTERS = {
    "active": [AgendaItem.Status.PENDING],
    "completed": [AgendaItem.Status.COMPLETED],
    "cancelled": [AgendaItem.Status.CANCELLED],
    "all": list(AgendaItem.Status.values),
}


def _list_items(args: dict, ctx: ToolContext) -> dict:
    _check_keys(args, {"date_from", "date_to", "type", "status", "include_undated", "query"})
    items = agenda.list_items(
        date_from=_opt_date(args, "date_from"),
        date_to=_opt_date(args, "date_to"),
        type=_enum(args, "type", set(AgendaItem.Type.values), nullable=True),
        statuses=_STATUS_FILTERS[_enum(args, "status", set(_STATUS_FILTERS))],
        include_undated=_bool(args, "include_undated"),
        query=_opt_str(args, "query", MAX_TITLE_LENGTH),
    )
    ctx.session.remember(*[item.id for item in reversed(items[:MAX_RECENT_LISTED])])
    return {"ok": True, "count": len(items), "items": [agenda.serialize_item(i) for i in items]}



def _get_item(args: dict, ctx: ToolContext) -> dict:
    _check_keys(args, {"item_id", "query", "include_inactive"})
    item_id = _opt_str(args, "item_id", 64)
    query = _opt_str(args, "query", MAX_TITLE_LENGTH)
    include_inactive = _bool(args, "include_inactive")

    if item_id:
        item = agenda.get_item(item_id)
        ctx.session.remember(item.id)
        return _item_result(item)
    if not query:
        raise ToolArgumentError("Provide either item_id or query.")

    statuses = _STATUS_FILTERS["all" if include_inactive else "active"]
    matches = agenda.find_items(query, statuses=statuses)
    if not matches:
        return {"ok": False, "error": "not_found", "message": f"No item matches {query!r}."}
    if len(matches) > 1:
        return {
            "ok": True,
            "ambiguous": True,
            "message": "Several items match. Ask the user which one they mean.",
            "candidates": [agenda.serialize_item(i) for i in matches],
        }
    ctx.session.remember(matches[0].id)
    return _item_result(matches[0], ambiguous=False)


def _update_item(args: dict, ctx: ToolContext) -> dict:
    _check_keys(args, {"item_id", "title", "description", "date", "time", "status"})
    item = agenda.get_item(_req_str(args, "item_id", 64))
    fields: dict[str, Any] = {}

    title = _opt_str(args, "title", MAX_TITLE_LENGTH)
    if title:
        fields["title"] = title
    description = _opt_str(args, "description")
    if description is not None:
        fields["description"] = description
    status = _enum(args, "status", {AgendaItem.Status.PENDING, AgendaItem.Status.CANCELLED},
                   nullable=True)
    if status:
        fields["status"] = status

    day = _opt_date(args, "date")
    at = _opt_time(args, "time")
    if day or at:
        current = item.when
        current_local = to_local(current) if current else None
        if item.type == AgendaItem.Type.APPOINTMENT:
            fields["starts_at"] = make_local_aware(
                day or current_local.date(), at or current_local.time().replace(second=0)
            )
        else:
            if day is None and current_local is None:
                raise ToolArgumentError(
                    "This task has no date; ask the user which day before setting a time."
                )
            new_day = day or current_local.date()
            if at is not None:
                new_time, has_time = at, True
            elif current_local is not None and item.due_has_time:
                new_time, has_time = current_local.time().replace(second=0), True
            else:
                new_time, has_time = time(0, 0), False
            fields["due_at"] = make_local_aware(new_day, new_time)
            fields["due_has_time"] = has_time

    if not fields:
        raise ToolArgumentError("No changes were provided.")

    updated = agenda.update_item(item.id, **fields)
    ctx.session.remember(updated.id)
    return _item_result(updated, updated=True, changed_fields=sorted(fields))


def _complete_item(args: dict, ctx: ToolContext) -> dict:
    _check_keys(args, {"item_id"})
    item = agenda.complete_item(_req_str(args, "item_id", 64))
    ctx.session.remember(item.id)
    return _item_result(item, completed=True)


def _delete_item(args: dict, ctx: ToolContext) -> dict:
    _check_keys(args, {"item_id", "confirmed"})
    item = agenda.get_item(_req_str(args, "item_id", 64))
    confirmed = _bool(args, "confirmed")
    session = ctx.session
    item_id = str(item.id)
    pending = session.pending_delete

    # Deletion only runs when the user confirmed in the turn right after the
    # confirmation request for this exact item. The model cannot self-confirm.
    if (
        confirmed
        and pending is not None
        and pending.item_id == item_id
        and pending.turn_no == session.turn_no - 1
    ):
        snapshot = agenda.delete_item(item_id)
        session.forget(item_id)
        return {"ok": True, "deleted": True, "item": snapshot}

    if pending is None or pending.item_id != item_id or pending.turn_no < session.turn_no - 1:
        session.pending_delete = PendingDelete(item_id=item_id, turn_no=session.turn_no)
    session.remember(item_id)
    return {
        "ok": False,
        "deleted": False,
        "error": "confirmation_required",
        "message": "Nothing was deleted. Ask the user to explicitly confirm deleting this "
                   "item; call delete_item with confirmed=true only after they confirm.",
        "item": agenda.serialize_item(item),
    }


HANDLERS: dict[str, Callable[[dict, ToolContext], dict]] = {
    "create_item": _create_item,
    "list_items": _list_items,
    "get_item": _get_item,
    "update_item": _update_item,
    "complete_item": _complete_item,
    "delete_item": _delete_item,
}


def execute_tool(name: str, raw_arguments: str, session: ConversationSession, clock=None) -> dict:
    """Validate and run one tool call. Never raises for expected failures."""
    handler = HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": "unknown_tool", "message": f"Unknown tool {name!r}."}
    try:
        args = json.loads(raw_arguments or "{}")
    except (TypeError, json.JSONDecodeError):
        return {"ok": False, "error": "invalid_arguments", "message": "Arguments are not valid JSON."}

    ctx = ToolContext(session, clock)
    try:
        return handler(args, ctx)
    except ToolArgumentError as exc:
        return {"ok": False, "error": "invalid_arguments", "message": str(exc)}
    except DateTimeValidationError as exc:
        return {"ok": False, "error": "invalid_datetime", "message": str(exc)}
    except agenda.AgendaError as exc:
        return {"ok": False, "error": exc.code, "message": str(exc)}
    except DatabaseError:
        logger.exception("Database error while executing tool %s", name)
        return {"ok": False, "error": "database_error",
                "message": "The database operation failed. Nothing was saved."}
