"""
Agenda business logic.

Plain Python/Django functions shared by the AI tools, the HTTP API, the admin
and the tests. Nothing here knows about OpenAI.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from typing import Iterable

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q

from assistant.models import AgendaItem

from .datetime_utils import format_local, local_range_bounds, to_local, WEEKDAYS_EN

logger = logging.getLogger("assistant.agenda")

UPDATABLE_FIELDS = {"title", "description", "starts_at", "due_at", "due_has_time", "type", "status"}


class AgendaError(Exception):
    code = "agenda_error"


class ItemNotFound(AgendaError):
    code = "not_found"


class InvalidAgendaData(AgendaError):
    code = "invalid_data"


class InvalidTransition(AgendaError):
    code = "invalid_status_transition"


def _validation_message(exc: ValidationError) -> str:
    if hasattr(exc, "message_dict"):
        return "; ".join(f"{field}: {' '.join(msgs)}" for field, msgs in exc.message_dict.items())
    return " ".join(exc.messages)


def _coerce_uuid(item_id) -> uuid.UUID:
    if isinstance(item_id, uuid.UUID):
        return item_id
    try:
        return uuid.UUID(str(item_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ItemNotFound(f"Invalid item id: {item_id!r}.") from exc


def serialize_item(item: AgendaItem) -> dict:
    when = item.when
    has_time = item.type == AgendaItem.Type.APPOINTMENT or item.due_has_time
    local = to_local(when) if when else None
    return {
        "id": str(item.id),
        "type": item.type,
        "title": item.title,
        "description": item.description,
        "status": item.status,
        "date": local.date().isoformat() if local else None,
        "time": local.strftime("%H:%M") if local and has_time else None,
        "weekday": WEEKDAYS_EN[local.weekday()] if local else None,
        "when": format_local(when, has_time),
        "starts_at": item.starts_at.isoformat() if item.starts_at else None,
        "due_at": item.due_at.isoformat() if item.due_at else None,
    }


def create_item(
    *,
    type: str,
    title: str,
    description: str = "",
    starts_at: datetime | None = None,
    due_at: datetime | None = None,
    due_has_time: bool = True,
) -> AgendaItem:
    if type not in AgendaItem.Type.values:
        raise InvalidAgendaData(f"Invalid type: {type!r}.")
    item = AgendaItem(
        type=type,
        title=title,
        description=description or "",
        starts_at=starts_at,
        due_at=due_at,
        due_has_time=due_has_time if due_at else True,
    )
    try:
        with transaction.atomic():
            item.full_clean()
            item.save()
    except ValidationError as exc:
        raise InvalidAgendaData(_validation_message(exc)) from exc
    logger.info("Created agenda item %s (%s)", item.id, item.type)
    return item


def get_item(item_id) -> AgendaItem:
    try:
        return AgendaItem.objects.get(pk=_coerce_uuid(item_id))
    except AgendaItem.DoesNotExist as exc:
        raise ItemNotFound(f"No agenda item with id {item_id}.") from exc


def _status_filter(statuses: Iterable[str] | None) -> list[str]:
    if statuses is None:
        return [AgendaItem.Status.PENDING]
    statuses = list(statuses)
    invalid = [s for s in statuses if s not in AgendaItem.Status.values]
    if invalid:
        raise InvalidAgendaData(f"Invalid status filter: {invalid}.")
    return statuses


def list_items(
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    type: str | None = None,
    statuses: Iterable[str] | None = None,
    include_undated: bool = False,
    query: str | None = None,
    limit: int = 50,
) -> list[AgendaItem]:
    """
    List agenda items. By default only active (PENDING) items are returned.

    ``date_from``/``date_to`` are inclusive local dates. If only one is given the
    range is open-ended on the other side. With a date filter, undated tasks are
    added only when ``include_undated`` is true; without one, all items match.
    """
    qs = AgendaItem.objects.with_effective_at().filter(status__in=_status_filter(statuses))
    if type:
        if type not in AgendaItem.Type.values:
            raise InvalidAgendaData(f"Invalid type: {type!r}.")
        qs = qs.filter(type=type)
    if query:
        qs = qs.filter(Q(title__icontains=query) | Q(description__icontains=query))

    if date_from or date_to:
        date_filter = Q()
        if date_from and date_to:
            start, end = local_range_bounds(date_from, date_to)
            date_filter = Q(effective_at__gte=start, effective_at__lt=end)
        elif date_from:
            date_filter = Q(effective_at__gte=local_range_bounds(date_from, date_from)[0])
        else:
            date_filter = Q(effective_at__lt=local_range_bounds(date_to, date_to)[1])
        if include_undated:
            date_filter |= Q(effective_at__isnull=True)
        qs = qs.filter(date_filter)

    qs = qs.order_by(F("effective_at").asc(nulls_last=True), "created_at")
    return list(qs[: max(1, min(limit, 200))])


def find_items(query: str, statuses: Iterable[str] | None = None, limit: int = 10) -> list[AgendaItem]:
    """Text search used to resolve which record the user means."""
    query = (query or "").strip()
    if not query:
        raise InvalidAgendaData("Search query cannot be empty.")
    return list_items(query=query, statuses=statuses, include_undated=True, limit=limit)


def update_item(item_id, **fields) -> AgendaItem:
    unknown = set(fields) - UPDATABLE_FIELDS
    if unknown:
        raise InvalidAgendaData(f"Fields cannot be updated: {sorted(unknown)}.")
    try:
        with transaction.atomic():
            try:
                item = AgendaItem.objects.select_for_update().get(pk=_coerce_uuid(item_id))
            except AgendaItem.DoesNotExist as exc:
                raise ItemNotFound(f"No agenda item with id {item_id}.") from exc

            new_status = fields.pop("status", None)
            if new_status is not None and new_status != item.status:
                if new_status not in AgendaItem.ALLOWED_TRANSITIONS.get(item.status, set()):
                    raise InvalidTransition(
                        f"Cannot change status from {item.status} to {new_status}."
                    )
                item.status = new_status

            for field, value in fields.items():
                setattr(item, field, value)
            item.full_clean()
            item.save()
    except ValidationError as exc:
        raise InvalidAgendaData(_validation_message(exc)) from exc
    logger.info("Updated agenda item %s", item.id)
    return item


def complete_item(item_id) -> AgendaItem:
    item = get_item(item_id)
    if item.status == AgendaItem.Status.COMPLETED:
        raise InvalidTransition("Item is already completed.")
    return update_item(item.id, status=AgendaItem.Status.COMPLETED)


def delete_item(item_id) -> dict:
    """Delete an item and return a snapshot of what was deleted."""
    with transaction.atomic():
        item = get_item(item_id)
        snapshot = serialize_item(item)
        item.delete()
    logger.info("Deleted agenda item %s", snapshot["id"])
    return snapshot
