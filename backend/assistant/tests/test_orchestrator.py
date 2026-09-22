import json
import uuid
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import httpx
import openai
from django.db import DatabaseError
from django.test import TestCase, override_settings

from assistant.models import AgendaItem
from assistant.services import agenda
from assistant.services.openai_client import (
    AssistantConfigError,
    AssistantUnavailableError,
    OpenAIResponsesClient,
    is_reasoning_model,
)
from assistant.services.orchestrator import Orchestrator, detect_language
from assistant.services.session import ConversationSession
from assistant.services.tools import TOOL_DEFINITIONS, execute_tool
from assistant.tests.helpers import (
    FakeResponsesClient,
    fixed_clock,
    function_call,
    message,
    reasoning,
    recent_ids,
    response,
    tool_outputs,
)

LONDON = ZoneInfo("Europe/London")


def aware(*args):
    return datetime(*args, tzinfo=LONDON)


def create_args(**overrides):
    args = {"type": "TASK", "title": "Sună la dentist", "description": None,
            "date": "2026-09-23", "time": "10:00"}
    args.update(overrides)
    return args


def list_args(**overrides):
    args = {"date_from": None, "date_to": None, "type": None, "status": "active",
            "include_undated": False, "query": None}
    args.update(overrides)
    return args


def update_args(item_id, **overrides):
    args = {"item_id": str(item_id), "title": None, "description": None, "date": None,
            "time": None, "status": None}
    args.update(overrides)
    return args


class OrchestratorTestCase(TestCase):
    def setUp(self):
        self.fake = FakeResponsesClient()
        self.orchestrator = Orchestrator(client=self.fake, clock=fixed_clock())
        self.session = ConversationSession()

    def say(self, text):
        return self.orchestrator.handle_message(self.session, text)


class ToolDispatchTests(OrchestratorTestCase):
    def test_create_task_dispatch_and_real_persistence(self):
        def reply(instructions, input_items):
            result = tool_outputs(input_items)[-1]
            self.assertTrue(result["ok"])
            self.assertEqual(result["item"]["time"], "10:00")
            return response(message("Am salvat taskul: Sună la dentist, mâine la ora 10."))

        self.fake.add(response(reasoning(), function_call("create_item", create_args())), reply)
        result = self.say("Amintește-mi mâine la 10 să sun la dentist.")

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Am salvat taskul: Sună la dentist, mâine la ora 10.")
        self.assertEqual(result.actions, [{"tool": "create_item", "ok": True, "error": None}])
        item = AgendaItem.objects.get()
        self.assertEqual(item.type, AgendaItem.Type.TASK)
        self.assertEqual(item.due_at, aware(2026, 9, 23, 10, 0))
        self.assertTrue(item.due_has_time)
        self.assertEqual(self.session.recent_items, [str(item.id)])

    def test_reasoning_and_function_call_items_replayed_without_server_storage(self):
        self.fake.add(response(reasoning("enc-123"), function_call("list_items", list_args())),
                      response(message("Nu ai nimic.")))
        self.say("Ce am de făcut?")
        second_input = self.fake.calls[1]["input"]
        types = [i.get("type", i.get("role")) for i in second_input]
        self.assertEqual(types, ["user", "reasoning", "function_call", "function_call_output"])
        self.assertEqual(second_input[1]["encrypted_content"], "enc-123")
        self.assertEqual(second_input[2]["id"], "fc_call_1")
        self.assertEqual(second_input[3]["call_id"], "call_1")

    def test_tools_are_strict_schemas(self):
        self.assertEqual({t["name"] for t in TOOL_DEFINITIONS},
                         {"create_item", "list_items", "get_item", "update_item",
                          "complete_item", "delete_item"})
        for tool in TOOL_DEFINITIONS:
            params = tool["parameters"]
            self.assertTrue(tool["strict"])
            self.assertFalse(params["additionalProperties"])
            self.assertEqual(set(params["required"]), set(params["properties"]))

    def test_list_query_answers_from_database(self):
        agenda.create_item(type="TASK", title="Sună la bancă", due_at=aware(2026, 9, 23, 9, 0))
        agenda.create_item(type="TASK", title="Altă zi", due_at=aware(2026, 9, 24, 9, 0))

        def reply(instructions, input_items):
            result = tool_outputs(input_items)[-1]
            self.assertEqual([i["title"] for i in result["items"]], ["Sună la bancă"])
            return response(message("Mâine trebuie să suni la bancă la 9."))

        self.fake.add(response(function_call(
            "list_items", list_args(date_from="2026-09-23", date_to="2026-09-23"))), reply)
        self.assertTrue(self.say("Ce am mâine?").ok)

    def test_empty_agenda_result_is_passed_to_model(self):
        def reply(instructions, input_items):
            self.assertEqual(tool_outputs(input_items)[-1], {"ok": True, "count": 0, "items": []})
            return response(message("Nu ai nimic mâine."))

        self.fake.add(response(function_call(
            "list_items", list_args(date_from="2026-09-23", date_to="2026-09-23"))), reply)
        self.assertEqual(self.say("Ce am mâine?").text, "Nu ai nimic mâine.")

    def test_multiple_tool_calls_in_one_response(self):
        self.fake.add(
            response(function_call("create_item", create_args(title="A"), "c1"),
                     function_call("create_item", create_args(title="B"), "c2")),
            response(message("Done.")),
        )
        result = self.say("Add A and B tomorrow at 10")
        self.assertEqual([a["ok"] for a in result.actions], [True, True])
        self.assertEqual(AgendaItem.objects.count(), 2)

    def test_instructions_contain_calendar_and_recent_items(self):
        item = agenda.create_item(type="TASK", title="Sună la bancă")
        self.session.remember(item.id)
        self.fake.add(response(message("ok")))
        self.say("salut")
        instructions = self.fake.calls[0]["instructions"]
        self.assertIn("Current local date: 2026-09-22 (Tuesday / marți)", instructions)
        self.assertIn(f"id={item.id}", instructions)
        self.assertIn("Sună la bancă", instructions)


class ValidationAndFailureTests(OrchestratorTestCase):
    def run_single_tool(self, name, args):
        captured = {}

        def reply(instructions, input_items):
            captured["result"] = tool_outputs(input_items)[-1]
            return response(message("Nu am putut face asta."))

        self.fake.add(response(function_call(name, args)), reply)
        result = self.say("test")
        return result, captured["result"]

    def test_invalid_time_rejected_and_nothing_saved(self):
        result, tool_result = self.run_single_tool("create_item", create_args(time="25:00"))
        self.assertEqual(tool_result["error"], "invalid_datetime")
        self.assertFalse(result.actions[0]["ok"])
        self.assertEqual(AgendaItem.objects.count(), 0)

    def test_invalid_date_format_rejected(self):
        _, tool_result = self.run_single_tool("create_item", create_args(date="25/09/2026"))
        self.assertEqual(tool_result["error"], "invalid_datetime")
        self.assertEqual(AgendaItem.objects.count(), 0)

    def test_malformed_json_arguments(self):
        _, tool_result = self.run_single_tool("create_item", "{not json")
        self.assertEqual(tool_result["error"], "invalid_arguments")

    def test_missing_and_extra_arguments(self):
        args = create_args()
        del args["time"]
        args["sql"] = "DROP TABLE"
        _, tool_result = self.run_single_tool("create_item", args)
        self.assertEqual(tool_result["error"], "invalid_arguments")
        self.assertEqual(AgendaItem.objects.count(), 0)

    def test_wrong_types(self):
        _, tool_result = self.run_single_tool("list_items", list_args(include_undated="yes"))
        self.assertEqual(tool_result["error"], "invalid_arguments")
        _, tool_result = self.run_single_tool("create_item", create_args(type="MEETING"))
        self.assertEqual(tool_result["error"], "invalid_arguments")
        _, tool_result = self.run_single_tool("create_item", create_args(title="   "))
        self.assertEqual(tool_result["error"], "invalid_arguments")

    def test_unknown_tool(self):
        _, tool_result = self.run_single_tool("run_python", {"code": "import os"})
        self.assertEqual(tool_result["error"], "unknown_tool")

    def test_appointment_without_time_not_saved(self):
        def reply(instructions, input_items):
            self.assertEqual(tool_outputs(input_items)[-1]["error"], "missing_appointment_time")
            return response(message("La ce oră este programarea?"))

        self.fake.add(response(function_call(
            "create_item", create_args(type="APPOINTMENT", title="Dentist", date="2026-09-25",
                                       time=None))), reply)
        result = self.say("Am programare la dentist vineri.")
        self.assertEqual(result.text, "La ce oră este programarea?")
        self.assertEqual(AgendaItem.objects.count(), 0)

    def test_appointment_without_date_not_saved(self):
        _, tool_result = self.run_single_tool(
            "create_item", create_args(type="APPOINTMENT", date=None, time="10:00"))
        self.assertEqual(tool_result["error"], "missing_appointment_date")

    def test_nonexistent_record(self):
        _, tool_result = self.run_single_tool("complete_item", {"item_id": str(uuid.uuid4())})
        self.assertEqual(tool_result["error"], "not_found")
        _, tool_result = self.run_single_tool("complete_item", {"item_id": "abc"})
        self.assertEqual(tool_result["error"], "not_found")

    def test_database_error_reported_as_failure(self):
        with mock.patch.object(agenda, "create_item", side_effect=DatabaseError("disk full")):
            result, tool_result = self.run_single_tool("create_item", create_args())
        self.assertEqual(tool_result["error"], "database_error")
        self.assertNotIn("disk full", tool_result["message"])
        self.assertFalse(result.actions[0]["ok"])

    def test_past_date_warning(self):
        _, tool_result = self.run_single_tool("create_item", create_args(date="2026-09-01"))
        self.assertTrue(tool_result["ok"])
        self.assertIn("past", tool_result["warning"])

    def test_iteration_limit(self):
        self.fake.add(*[response(function_call("list_items", list_args(), f"c{i}"))
                        for i in range(6)])
        result = self.say("Ce am de făcut?")
        self.assertFalse(result.ok)
        self.assertEqual(len(self.fake.calls), 6)
        self.assertIn("Nu am putut finaliza", result.text)

    def test_api_error_before_any_tool(self):
        self.fake.add(AssistantUnavailableError("The AI service is temporarily unavailable."))
        result = self.say("Ce am mâine?")
        self.assertFalse(result.ok)
        self.assertIn("nu este disponibil", result.text)
        self.assertEqual(self.session.turns, [])  # failed turn not added to context

    def test_api_error_after_successful_write_is_reported_honestly(self):
        self.fake.add(response(function_call("create_item", create_args())),
                      AssistantUnavailableError("The AI service is temporarily unavailable."))
        result = self.say("Adaugă un task mâine la 10")
        self.assertFalse(result.ok)
        self.assertEqual(AgendaItem.objects.count(), 1)
        self.assertIn("salvate în agendă: creare", result.text)
        self.assertNotIn("create_item", result.text)  # no internal names shown to the user

    def test_missing_api_key_is_friendly_error(self):
        orchestrator = Orchestrator(client=OpenAIResponsesClient(api_key=""),
                                    clock=fixed_clock())
        result = orchestrator.handle_message(self.session, "What do I have tomorrow?")
        self.assertFalse(result.ok)
        self.assertIn("OPENAI_API_KEY", result.text)

    def test_empty_and_too_long_messages(self):
        self.assertFalse(self.say("   ").ok)
        self.assertFalse(self.say("x" * 1001).ok)
        self.assertEqual(self.fake.calls, [])


class FollowUpTests(OrchestratorTestCase):
    def test_follow_up_references_use_recent_item_ids(self):
        # Turn 1: create.
        self.fake.add(response(function_call("create_item", create_args(title="Sună la bancă"))),
                      response(message("Am salvat taskul.")))
        self.say("Adaugă un task să sun la bancă mâine la 10.")
        item = AgendaItem.objects.get()

        # Turn 2: "Mută-l la ora 12" -> the model resolves "-l" from the injected recent ids.
        def move(instructions, input_items):
            self.assertEqual(recent_ids(instructions)[0], str(item.id))
            return response(function_call("update_item",
                                          update_args(recent_ids(instructions)[0], time="12:00")))

        self.fake.add(move, response(message("Am mutat taskul la ora 12.")))
        self.assertTrue(self.say("Mută-l la ora 12.").ok)
        item.refresh_from_db()
        self.assertEqual(item.due_at, aware(2026, 9, 23, 12, 0))  # date kept

        # Previous turn is in the history, compacted (no reasoning, no item ids).
        history = self.fake.calls[2]["input"]
        self.assertEqual(history[0], {"role": "user",
                                      "content": "Adaugă un task să sun la bancă mâine la 10."})
        self.assertIn({"role": "assistant", "content": "Am salvat taskul."}, history)
        self.assertFalse(any(i.get("type") == "reasoning" for i in history))

        # Turn 3: "Mută-l pe vineri" keeps the time.
        self.fake.add(
            lambda ins, inp: response(function_call(
                "update_item", update_args(recent_ids(ins)[0], date="2026-09-25"))),
            response(message("Am mutat taskul pe vineri.")))
        self.say("Mută-l pe vineri.")
        item.refresh_from_db()
        self.assertEqual(item.due_at, aware(2026, 9, 25, 12, 0))

        # Turn 4: complete.
        self.fake.add(
            lambda ins, inp: response(function_call("complete_item",
                                                    {"item_id": recent_ids(ins)[0]})),
            response(message("Am marcat taskul ca finalizat.")))
        self.say("Marchează-l ca finalizat.")
        item.refresh_from_db()
        self.assertEqual(item.status, AgendaItem.Status.COMPLETED)

    def test_history_is_bounded(self):
        for i in range(12):
            self.fake.add(response(message(f"r{i}")))
            self.say(f"mesaj {i}")
        self.assertEqual(len(self.session.turns), self.session.max_turns)
        self.assertEqual(self.session.turns[0][0]["content"], "mesaj 4")


class AmbiguityAndDeleteTests(OrchestratorTestCase):
    def test_ambiguous_selection_returns_candidates_and_changes_nothing(self):
        agenda.create_item(type="TASK", title="Sună la dentist")
        agenda.create_item(type="APPOINTMENT", title="Dentist", starts_at=aware(2026, 9, 25, 10))

        def reply(instructions, input_items):
            result = tool_outputs(input_items)[-1]
            self.assertTrue(result["ambiguous"])
            self.assertEqual(len(result["candidates"]), 2)
            return response(message("Care dintre ele? Taskul sau programarea?"))

        self.fake.add(response(function_call(
            "get_item", {"item_id": None, "query": "dentist", "include_inactive": False})), reply)
        result = self.say("Șterge dentistul.")
        self.assertEqual(result.actions[0]["tool"], "get_item")
        self.assertEqual(AgendaItem.objects.count(), 2)
        self.assertEqual(self.session.recent_items, [])

    def test_get_item_single_match_and_by_id(self):
        item = agenda.create_item(type="TASK", title="Sună la dentist")
        session = ConversationSession()
        result = execute_tool("get_item", json.dumps(
            {"item_id": None, "query": "dentist", "include_inactive": False}), session)
        self.assertFalse(result["ambiguous"])
        self.assertEqual(result["item"]["id"], str(item.id))
        result = execute_tool("get_item", json.dumps(
            {"item_id": str(item.id), "query": None, "include_inactive": False}), session)
        self.assertTrue(result["ok"])
        result = execute_tool("get_item", json.dumps(
            {"item_id": None, "query": None, "include_inactive": False}), session)
        self.assertEqual(result["error"], "invalid_arguments")

    def test_delete_requires_confirmation_in_a_later_turn(self):
        item = agenda.create_item(type="TASK", title="Sună la bancă")
        args = {"item_id": str(item.id)}

        # Turn 1: the model asks and even tries to self-confirm in the same turn.
        def check_refused(instructions, input_items):
            outputs = tool_outputs(input_items)
            self.assertTrue(all(o["error"] == "confirmation_required" for o in outputs))
            return response(message("Sigur vrei să șterg taskul „Sună la bancă”?"))

        self.fake.add(
            response(function_call("delete_item", {**args, "confirmed": False}, "c1"),
                     function_call("delete_item", {**args, "confirmed": True}, "c2")),
            check_refused)
        self.say("Șterge taskul cu banca.")
        self.assertTrue(AgendaItem.objects.filter(pk=item.pk).exists())

        # Turn 2: the user confirms.
        def check_deleted(instructions, input_items):
            self.assertTrue(tool_outputs(input_items)[-1]["deleted"])
            return response(message("Am șters taskul."))

        self.fake.add(response(function_call("delete_item", {**args, "confirmed": True})),
                      check_deleted)
        self.assertEqual(self.say("Da.").text, "Am șters taskul.")
        self.assertFalse(AgendaItem.objects.filter(pk=item.pk).exists())
        self.assertNotIn(str(item.id), self.session.recent_items)

    def test_delete_confirmed_without_prior_request_is_refused(self):
        item = agenda.create_item(type="TASK", title="x")
        session = ConversationSession()
        session.start_turn()
        result = execute_tool("delete_item", json.dumps(
            {"item_id": str(item.id), "confirmed": True}), session)
        self.assertEqual(result["error"], "confirmation_required")
        self.assertTrue(AgendaItem.objects.filter(pk=item.pk).exists())

    def test_delete_confirmation_expires(self):
        item = agenda.create_item(type="TASK", title="x")
        session = ConversationSession()
        payload = lambda confirmed: json.dumps({"item_id": str(item.id), "confirmed": confirmed})
        session.start_turn()
        execute_tool("delete_item", payload(False), session)
        session.start_turn()
        session.start_turn()  # user talked about something else in between
        result = execute_tool("delete_item", payload(True), session)
        self.assertEqual(result["error"], "confirmation_required")
        self.assertTrue(AgendaItem.objects.filter(pk=item.pk).exists())


class UpdateToolTests(TestCase):
    def setUp(self):
        self.session = ConversationSession()

    def update(self, item, **kw):
        return execute_tool("update_item", json.dumps(update_args(item.id, **kw)), self.session)

    def test_time_only_on_appointment_keeps_date(self):
        item = agenda.create_item(type="APPOINTMENT", title="Dentist",
                                  starts_at=aware(2026, 9, 25, 10))
        self.assertTrue(self.update(item, time="12:00")["ok"])
        item.refresh_from_db()
        self.assertEqual(item.starts_at, aware(2026, 9, 25, 12))

    def test_date_only_task_gets_time(self):
        item = agenda.create_item(type="TASK", title="x", due_at=aware(2026, 9, 24, 0),
                                  due_has_time=False)
        self.update(item, time="09:30")
        item.refresh_from_db()
        self.assertEqual(item.due_at, aware(2026, 9, 24, 9, 30))
        self.assertTrue(item.due_has_time)

    def test_time_on_undated_task_requires_date(self):
        item = agenda.create_item(type="TASK", title="x")
        result = self.update(item, time="09:30")
        self.assertEqual(result["error"], "invalid_arguments")

    def test_cancel_and_no_changes(self):
        item = agenda.create_item(type="TASK", title="x")
        self.assertEqual(self.update(item, status="CANCELLED")["item"]["status"], "CANCELLED")
        self.assertEqual(self.update(item)["error"], "invalid_arguments")

    def test_create_date_only_task(self):
        result = execute_tool("create_item", json.dumps(create_args(time=None)), self.session)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["item"]["time"])
        self.assertFalse(AgendaItem.objects.get().due_has_time)

    def test_create_on_dst_gap_rejected(self):
        result = execute_tool("create_item", json.dumps(
            create_args(date="2027-03-28", time="01:30")), self.session)
        self.assertEqual(result["error"], "invalid_datetime")


class OpenAIClientTests(TestCase):
    def test_request_uses_store_false_and_encrypted_reasoning(self):
        client = OpenAIResponsesClient(api_key="test", model="gpt-5.6-luna", reasoning_effort="low")
        request = client.build_request("inst", [{"role": "user", "content": "hi"}], TOOL_DEFINITIONS)
        self.assertIs(request["store"], False)
        self.assertEqual(request["include"], ["reasoning.encrypted_content"])
        self.assertEqual(request["reasoning"], {"effort": "low"})
        self.assertEqual(request["model"], "gpt-5.6-luna")

    def test_non_reasoning_model_omits_reasoning_params(self):
        client = OpenAIResponsesClient(api_key="test", model="gpt-4.1-mini", reasoning_effort="low")
        request = client.build_request("inst", [], [])
        self.assertIs(request["store"], False)
        self.assertNotIn("reasoning", request)
        self.assertNotIn("include", request)
        self.assertFalse(is_reasoning_model("gpt-4o"))
        self.assertTrue(is_reasoning_model("gpt-6-astra"))

    @override_settings(OPENAI_MODEL="configured-model")
    def test_model_is_configurable(self):
        self.assertEqual(OpenAIResponsesClient(api_key="k").model, "configured-model")

    def test_sdk_called_with_store_false(self):
        client = OpenAIResponsesClient(api_key="test", model="gpt-5.6-luna")
        client._client = mock.MagicMock()
        client.create_response("inst", [], TOOL_DEFINITIONS)
        kwargs = client._client.responses.create.call_args.kwargs
        self.assertIs(kwargs["store"], False)

    def test_missing_key(self):
        with self.assertRaises(AssistantConfigError):
            OpenAIResponsesClient(api_key="").create_response("i", [], [])

    def test_error_mapping(self):
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")

        def status_error(cls, code):
            return cls("boom", response=httpx.Response(code, request=request), body=None)

        cases = [
            (status_error(openai.AuthenticationError, 401), AssistantConfigError),
            (status_error(openai.NotFoundError, 404), AssistantConfigError),
            (status_error(openai.BadRequestError, 400), AssistantConfigError),
            (status_error(openai.RateLimitError, 429), AssistantUnavailableError),
            (status_error(openai.InternalServerError, 500), AssistantUnavailableError),
            (openai.APIConnectionError(request=request), AssistantUnavailableError),
        ]
        for exc, expected in cases:
            client = OpenAIResponsesClient(api_key="sk-secret-value", model="gpt-5.6-luna")
            client._client = mock.MagicMock()
            client._client.responses.create.side_effect = exc
            with self.assertRaises(expected) as ctx:
                client.create_response("i", [], [])
            self.assertNotIn("sk-secret-value", str(ctx.exception))


class LanguageDetectionTests(TestCase):
    def test_detect_language(self):
        self.assertEqual(detect_language("Ce am de făcut mâine?"), "ro")
        self.assertEqual(detect_language("Ce am maine"), "ro")
        self.assertEqual(detect_language("What do I have tomorrow?"), "en")
