import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.test import TestCase

from assistant.models import AgendaItem
from assistant.services import agenda

LONDON = ZoneInfo("Europe/London")


def aware(*args):
    return datetime(*args, tzinfo=LONDON)


class AgendaServiceTests(TestCase):
    def setUp(self):
        self.task = agenda.create_item(type="TASK", title="Sună la dentist",
                                       due_at=aware(2026, 9, 23, 10, 0))
        self.appt = agenda.create_item(type="APPOINTMENT", title="Dentist",
                                       starts_at=aware(2026, 9, 25, 14, 30))
        self.undated = agenda.create_item(type="TASK", title="Citește cartea")

    def test_create_persists(self):
        self.assertEqual(AgendaItem.objects.count(), 3)
        stored = AgendaItem.objects.get(pk=self.task.pk)
        self.assertEqual(stored.title, "Sună la dentist")
        self.assertEqual(stored.due_at, aware(2026, 9, 23, 10, 0))

    def test_create_invalid_raises(self):
        with self.assertRaises(agenda.InvalidAgendaData):
            agenda.create_item(type="APPOINTMENT", title="No time")
        with self.assertRaises(agenda.InvalidAgendaData):
            agenda.create_item(type="TASK", title="  ")
        with self.assertRaises(agenda.InvalidAgendaData):
            agenda.create_item(type="OTHER", title="x")
        self.assertEqual(AgendaItem.objects.count(), 3)

    def test_list_by_single_day(self):
        items = agenda.list_items(date_from=date(2026, 9, 23), date_to=date(2026, 9, 23))
        self.assertEqual([i.pk for i in items], [self.task.pk])

    def test_list_by_range_and_type(self):
        items = agenda.list_items(date_from=date(2026, 9, 21), date_to=date(2026, 9, 27))
        self.assertEqual([i.pk for i in items], [self.task.pk, self.appt.pk])
        appts = agenda.list_items(date_from=date(2026, 9, 21), date_to=date(2026, 9, 27),
                                  type="APPOINTMENT")
        self.assertEqual([i.pk for i in appts], [self.appt.pk])

    def test_list_include_undated(self):
        items = agenda.list_items(date_from=date(2026, 9, 23), date_to=date(2026, 9, 23),
                                  include_undated=True)
        self.assertEqual({i.pk for i in items}, {self.task.pk, self.undated.pk})

    def test_list_without_filters_returns_all_active(self):
        self.assertEqual(len(agenda.list_items()), 3)

    def test_list_default_excludes_completed_but_available_on_request(self):
        agenda.complete_item(self.task.pk)
        self.assertNotIn(self.task.pk, [i.pk for i in agenda.list_items()])
        completed = agenda.list_items(statuses=[AgendaItem.Status.COMPLETED])
        self.assertEqual([i.pk for i in completed], [self.task.pk])

    def test_list_query(self):
        items = agenda.list_items(query="dentist")
        self.assertEqual({i.pk for i in items}, {self.task.pk, self.appt.pk})

    def test_get_item_and_not_found(self):
        self.assertEqual(agenda.get_item(str(self.task.pk)).pk, self.task.pk)
        with self.assertRaises(agenda.ItemNotFound):
            agenda.get_item(uuid.uuid4())
        with self.assertRaises(agenda.ItemNotFound):
            agenda.get_item("not-a-uuid")

    def test_find_items(self):
        self.assertEqual(len(agenda.find_items("dentist")), 2)
        with self.assertRaises(agenda.InvalidAgendaData):
            agenda.find_items("  ")

    def test_update_item(self):
        agenda.update_item(self.task.pk, title="Sună la bancă", due_at=aware(2026, 9, 23, 12, 0))
        stored = AgendaItem.objects.get(pk=self.task.pk)
        self.assertEqual(stored.title, "Sună la bancă")
        self.assertEqual(stored.due_at, aware(2026, 9, 23, 12, 0))

    def test_update_invalid_rolls_back(self):
        with self.assertRaises(agenda.InvalidAgendaData):
            agenda.update_item(self.appt.pk, title="New", starts_at=None)
        stored = AgendaItem.objects.get(pk=self.appt.pk)
        self.assertEqual(stored.title, "Dentist")
        self.assertIsNotNone(stored.starts_at)

    def test_update_rejects_unknown_fields(self):
        with self.assertRaises(agenda.InvalidAgendaData):
            agenda.update_item(self.task.pk, id=uuid.uuid4())

    def test_update_invalid_transition(self):
        agenda.complete_item(self.task.pk)
        with self.assertRaises(agenda.InvalidTransition):
            agenda.update_item(self.task.pk, status=AgendaItem.Status.CANCELLED)

    def test_complete_item(self):
        agenda.complete_item(self.task.pk)
        self.assertEqual(AgendaItem.objects.get(pk=self.task.pk).status,
                         AgendaItem.Status.COMPLETED)
        with self.assertRaises(agenda.InvalidTransition):
            agenda.complete_item(self.task.pk)

    def test_delete_item(self):
        snapshot = agenda.delete_item(self.task.pk)
        self.assertEqual(snapshot["title"], "Sună la dentist")
        self.assertFalse(AgendaItem.objects.filter(pk=self.task.pk).exists())
        with self.assertRaises(agenda.ItemNotFound):
            agenda.delete_item(self.task.pk)

    def test_serialize_item_local_time(self):
        data = agenda.serialize_item(self.appt)
        self.assertEqual(data["date"], "2026-09-25")
        self.assertEqual(data["time"], "14:30")
        self.assertEqual(data["weekday"], "Friday")
        self.assertEqual(data["when"], "Friday 25/09/2026 14:30")

    def test_serialize_date_only_task(self):
        item = agenda.create_item(type="TASK", title="x", due_at=aware(2026, 9, 24, 0, 0),
                                  due_has_time=False)
        data = agenda.serialize_item(item)
        self.assertEqual(data["date"], "2026-09-24")
        self.assertIsNone(data["time"])
        self.assertEqual(data["when"], "Thursday 24/09/2026")
