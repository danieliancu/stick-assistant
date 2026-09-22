"""Regression tests for defects found in the Stage 1 audit."""

import json
from datetime import date, datetime
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import httpx
import openai
from django.test import TestCase

from assistant.models import AgendaItem
from assistant.services import agenda, tools
from assistant.services.openai_client import OpenAIResponsesClient
from assistant.services.orchestrator import Orchestrator
from assistant.services.session import ConversationSession, PendingDelete
from assistant.services.tools import execute_tool, is_explicit_confirmation
from assistant.tests.helpers import (
    FakeResponsesClient,
    fixed_clock,
    function_call,
    message,
    response,
    tool_outputs,
)

LONDON = ZoneInfo("Europe/London")


def aware(*args):
    return datetime(*args, tzinfo=LONDON)


class Base(TestCase):
    def setUp(self):
        self.fake = FakeResponsesClient()
        self.orchestrator = Orchestrator(client=self.fake, clock=fixed_clock())
        self.session = ConversationSession()

    def say(self, text):
        return self.orchestrator.handle_message(self.session, text)


class DeleteConfirmationTests(Base):
    """P0: the model's confirmed=true flag must not delete unless the user said yes."""

    def setUp(self):
        super().setUp()
        self.item = agenda.create_item(type="TASK", title="Sună la bancă")
        self.args = {"item_id": str(self.item.id)}

    def ask_for_confirmation(self):
        self.fake.add(response(function_call("delete_item", {**self.args, "confirmed": False})),
                      response(message("Sigur vrei să îl șterg?")))
        self.say("Șterge taskul cu banca.")

    def test_user_declines_but_model_confirms_nothing_deleted(self):
        self.ask_for_confirmation()

        def check(instructions, input_items):
            self.assertEqual(tool_outputs(input_items)[-1]["error"], "not_confirmed")
            return response(message("Bine, nu îl șterg."))

        self.fake.add(response(function_call("delete_item", {**self.args, "confirmed": True})),
                      check)
        self.say("Nu, lasă.")
        self.assertTrue(AgendaItem.objects.filter(pk=self.item.pk).exists())
        self.assertIsNone(self.session.pending_delete)

    def test_declined_delete_cannot_execute_on_a_later_yes(self):
        self.ask_for_confirmation()
        self.fake.add(response(message("Bine.")))
        self.say("Nu.")
        # Later the model tries to reuse the old request after an unrelated "da".
        self.fake.add(response(function_call("delete_item", {**self.args, "confirmed": True})),
                      response(message("...")))
        self.say("Da, mulțumesc.")
        self.assertTrue(AgendaItem.objects.filter(pk=self.item.pk).exists())

    def test_confirmation_request_expires_after_one_turn(self):
        self.session.turn_no = 1
        self.session.pending_delete = PendingDelete(str(self.item.id), turn_no=1)
        self.session.start_turn("altceva")
        self.assertIsNotNone(self.session.pending_delete)
        self.session.start_turn("da")
        self.assertIsNone(self.session.pending_delete)

    def test_explicit_yes_deletes(self):
        self.ask_for_confirmation()
        self.fake.add(response(function_call("delete_item", {**self.args, "confirmed": True})),
                      response(message("Am șters taskul.")))
        self.say("Da, șterge-l.")
        self.assertFalse(AgendaItem.objects.filter(pk=self.item.pk).exists())

    def test_confirmation_detector(self):
        for text in ("Da.", "da, șterge-l", "Yes", "ok", "sure, go ahead", "Confirm"):
            self.assertTrue(is_explicit_confirmation(text), text)
        for text in ("Nu", "no, wait", "Da, dar nu acum", "ce?", "", "stai puțin", "cancel"):
            self.assertFalse(is_explicit_confirmation(text), text)


class ModelOutputFailureTests(Base):
    """P1: empty/incomplete model output or tool crashes must not look like success."""

    def test_empty_final_output_is_an_error(self):
        self.fake.add(SimpleNamespace(output=[], status="incomplete"))
        result = self.say("Ce am mâine?")
        self.assertFalse(result.ok)
        self.assertEqual(self.session.turns, [])

    def test_empty_output_after_write_reports_saved_operation(self):
        self.fake.add(response(function_call("create_item", {
            "type": "TASK", "title": "X", "description": None, "date": None, "time": None})),
            SimpleNamespace(output=[], status="incomplete"))
        result = self.say("Add task X")
        self.assertFalse(result.ok)
        self.assertIn("saved to the agenda: create", result.text)
        self.assertEqual(AgendaItem.objects.count(), 1)

    def test_refusal_text_is_returned(self):
        refusal = SimpleNamespace(type="message", id="m", content=[
            SimpleNamespace(type="refusal", refusal="I can't help with that.")])
        self.fake.add(response(refusal))
        self.assertEqual(self.say("something odd").text, "I can't help with that.")

    def test_unexpected_tool_exception_is_contained(self):
        def check(instructions, input_items):
            self.assertEqual(tool_outputs(input_items)[-1]["error"], "internal_error")
            return response(message("Nu am putut."))

        with mock.patch.dict(tools.HANDLERS, {"list_items": mock.Mock(side_effect=TypeError)}):
            self.fake.add(response(function_call("list_items", {})), check)
            result = self.say("Ce am?")
        self.assertTrue(result.ok)
        self.assertEqual(result.actions[0]["error"], "internal_error")

    def test_invalid_model_configuration_is_reported(self):
        client = OpenAIResponsesClient(api_key="k", model="no-such-model")
        client._client = mock.MagicMock()
        req = httpx.Request("POST", "https://api.openai.com/v1/responses")
        client._client.responses.create.side_effect = openai.NotFoundError(
            "model not found", response=httpx.Response(404, request=req), body=None)
        result = Orchestrator(client=client, clock=fixed_clock()).handle_message(
            self.session, "What do I have today?")
        self.assertFalse(result.ok)
        self.assertIn("no-such-model", result.text)
        self.assertIn("OPENAI_MODEL", result.text)


class ContextAndPersistenceTests(Base):
    def test_new_conversation_still_sees_saved_records(self):
        self.fake.add(response(function_call("create_item", {
            "type": "TASK", "title": "Sună la bancă", "description": None,
            "date": "2026-09-23", "time": "10:00"})), response(message("Salvat.")))
        self.say("Adaugă task mâine la 10 să sun la bancă")

        # Brand-new session (e.g. after restarting the terminal): no memory, same database.
        fresh = ConversationSession()

        def check(instructions, input_items):
            self.assertIn("Recently referenced items: none.", instructions)
            self.assertEqual(tool_outputs(input_items)[-1]["items"][0]["title"], "Sună la bancă")
            return response(message("Mâine: sună la bancă la 10."))

        self.fake.add(response(function_call("list_items", {
            "date_from": "2026-09-23", "date_to": "2026-09-23", "type": None,
            "status": "active", "include_undated": False, "query": None})), check)
        self.assertTrue(self.orchestrator.handle_message(fresh, "Ce am mâine?").ok)

    def test_invented_uuid_changes_nothing(self):
        agenda.create_item(type="TASK", title="x")
        result = execute_tool("complete_item", json.dumps(
            {"item_id": "00000000-0000-4000-8000-000000000000"}), self.session)
        self.assertEqual(result["error"], "not_found")
        self.assertEqual(AgendaItem.objects.filter(status="COMPLETED").count(), 0)

    def test_duplicate_titles_are_ambiguous(self):
        agenda.create_item(type="TASK", title="Sună la bancă")
        agenda.create_item(type="TASK", title="Sună la bancă")
        result = execute_tool("get_item", json.dumps(
            {"item_id": None, "query": "bancă", "include_inactive": False}), self.session)
        self.assertTrue(result["ambiguous"])
        self.assertEqual(len(result["candidates"]), 2)


class DateBoundaryTests(TestCase):
    def test_midnight_boundaries(self):
        late = agenda.create_item(type="TASK", title="late", due_at=aware(2026, 9, 23, 23, 59))
        agenda.create_item(type="TASK", title="next", due_at=aware(2026, 9, 24, 0, 0))
        early = agenda.create_item(type="TASK", title="early", due_at=aware(2026, 9, 23, 0, 0))
        items = agenda.list_items(date_from=date(2026, 9, 23), date_to=date(2026, 9, 23))
        self.assertEqual([i.pk for i in items], [early.pk, late.pk])

    def test_listing_on_dst_days(self):
        # 25 Oct 2026 has 25 hours; both 00:30 (BST) and 23:30 (GMT) belong to it.
        first = agenda.create_item(type="TASK", title="a", due_at=aware(2026, 10, 25, 0, 30))
        last = agenda.create_item(type="TASK", title="b", due_at=aware(2026, 10, 25, 23, 30))
        items = agenda.list_items(date_from=date(2026, 10, 25), date_to=date(2026, 10, 25))
        self.assertEqual([i.pk for i in items], [first.pk, last.pk])
        # 29 Mar 2026 has 23 hours.
        spring = agenda.create_item(type="TASK", title="c", due_at=aware(2026, 3, 29, 23, 30))
        items = agenda.list_items(date_from=date(2026, 3, 29), date_to=date(2026, 3, 29))
        self.assertEqual([i.pk for i in items], [spring.pk])

    def test_ambiguous_autumn_time_warns(self):
        result = execute_tool("create_item", json.dumps({
            "type": "APPOINTMENT", "title": "Night", "description": None,
            "date": "2026-10-25", "time": "01:30"}), ConversationSession(), clock=fixed_clock())
        self.assertTrue(result["ok"])
        self.assertIn("occurs twice", result["warning"])
        # First occurrence = 01:30 BST = 00:30 UTC.
        self.assertEqual(AgendaItem.objects.get().starts_at,
                         datetime(2026, 10, 25, 0, 30, tzinfo=ZoneInfo("UTC")))

    def test_unambiguous_time_has_no_warning(self):
        result = execute_tool("create_item", json.dumps({
            "type": "APPOINTMENT", "title": "Day", "description": None,
            "date": "2026-10-25", "time": "10:00"}), ConversationSession(), clock=fixed_clock())
        self.assertNotIn("warning", result)
