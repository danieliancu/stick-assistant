from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.test import TestCase

from assistant.models import AgendaItem

LONDON = ZoneInfo("Europe/London")


def aware(*args):
    return datetime(*args, tzinfo=LONDON)


class AgendaItemModelTests(TestCase):
    def test_create_task_without_date(self):
        item = AgendaItem(type=AgendaItem.Type.TASK, title="Buy milk")
        item.full_clean()
        item.save()
        self.assertEqual(item.status, AgendaItem.Status.PENDING)
        self.assertIsNotNone(item.created_at)
        self.assertIsNotNone(item.updated_at)

    def test_create_appointment(self):
        item = AgendaItem(type=AgendaItem.Type.APPOINTMENT, title="Dentist",
                          starts_at=aware(2026, 9, 25, 10, 0))
        item.full_clean()
        item.save()
        self.assertEqual(AgendaItem.objects.get(pk=item.pk).starts_at, aware(2026, 9, 25, 10, 0))

    def test_appointment_requires_starts_at(self):
        item = AgendaItem(type=AgendaItem.Type.APPOINTMENT, title="Dentist")
        with self.assertRaises(ValidationError) as ctx:
            item.full_clean()
        self.assertIn("starts_at", ctx.exception.message_dict)

    def test_appointment_rejects_due_at(self):
        item = AgendaItem(type=AgendaItem.Type.APPOINTMENT, title="Dentist",
                          starts_at=aware(2026, 9, 25, 10), due_at=aware(2026, 9, 25, 10))
        with self.assertRaises(ValidationError):
            item.full_clean()

    def test_empty_title_rejected(self):
        for title in ("", "   "):
            with self.assertRaises(ValidationError) as ctx:
                AgendaItem(type=AgendaItem.Type.TASK, title=title).full_clean()
            self.assertIn("title", ctx.exception.message_dict)

    def test_naive_datetime_rejected(self):
        item = AgendaItem(type=AgendaItem.Type.TASK, title="x", due_at=datetime(2026, 9, 25, 10))
        with self.assertRaises(ValidationError) as ctx:
            item.full_clean()
        self.assertIn("due_at", ctx.exception.message_dict)

    def test_invalid_datetime_value_rejected(self):
        item = AgendaItem(type=AgendaItem.Type.TASK, title="x", due_at="not a date")
        with self.assertRaises(ValidationError):
            item.full_clean()

    def test_invalid_type_rejected(self):
        with self.assertRaises(ValidationError):
            AgendaItem(type="MEETING", title="x").full_clean()

    def test_valid_status_transitions(self):
        item = AgendaItem.objects.create(type=AgendaItem.Type.TASK, title="x")
        item.status = AgendaItem.Status.COMPLETED
        item.save()
        item.status = AgendaItem.Status.PENDING
        item.save()
        item.status = AgendaItem.Status.CANCELLED
        item.save()
        self.assertEqual(AgendaItem.objects.get(pk=item.pk).status, AgendaItem.Status.CANCELLED)

    def test_invalid_status_transition_rejected(self):
        item = AgendaItem.objects.create(type=AgendaItem.Type.TASK, title="x",
                                         status=AgendaItem.Status.COMPLETED)
        item.status = AgendaItem.Status.CANCELLED
        with self.assertRaises(ValidationError):
            item.save()
        self.assertEqual(AgendaItem.objects.get(pk=item.pk).status, AgendaItem.Status.COMPLETED)

    def test_unknown_status_rejected(self):
        item = AgendaItem(type=AgendaItem.Type.TASK, title="x", status="DONE")
        with self.assertRaises(ValidationError):
            item.save()

    def test_persistence_and_active_queryset(self):
        pending = AgendaItem.objects.create(type=AgendaItem.Type.TASK, title="a")
        AgendaItem.objects.create(type=AgendaItem.Type.TASK, title="b",
                                  status=AgendaItem.Status.COMPLETED)
        # Re-read from the database, not from the Python object.
        self.assertEqual(list(AgendaItem.objects.active().values_list("pk", flat=True)),
                         [pending.pk])
        self.assertEqual(AgendaItem.objects.count(), 2)

    def test_uuid_primary_key(self):
        item = AgendaItem.objects.create(type=AgendaItem.Type.TASK, title="a")
        self.assertEqual(len(str(item.pk)), 36)
