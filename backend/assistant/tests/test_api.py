import json
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import httpx
import openai
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse

from assistant import views
from assistant.models import AgendaItem
from assistant.services import agenda
from assistant.services.openai_client import OpenAIResponsesClient
from assistant.services.orchestrator import Orchestrator
from assistant.services.session import DEVICE_SESSION_LOCK, reset_device_session
from assistant.tests.helpers import (
    FIXED_NOW,
    FakeResponsesClient,
    fixed_clock,
    function_call,
    message,
    response,
)

LONDON = ZoneInfo("Europe/London")
TOKEN = "test-device-token"


def aware(*args):
    return datetime(*args, tzinfo=LONDON)


# SECURE_SSL_REDIRECT is on when DJANGO_DEBUG is false (e.g. in CI); disable it here so
# the suite behaves the same in every environment. The redirect has its own test below.
@override_settings(DEVICE_API_TOKEN=TOKEN, SECURE_SSL_REDIRECT=False)
class ApiTestCase(TestCase):
    def setUp(self):
        reset_device_session()
        self.fake = FakeResponsesClient()
        patcher = mock.patch.object(
            views, "get_orchestrator",
            return_value=Orchestrator(client=self.fake, clock=fixed_clock()))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(reset_device_session)

    def post_chat(self, body, token=TOKEN, raw=False):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        data = body if raw else json.dumps(body)
        return self.client.post(reverse("assistant:chat"), data=data,
                                content_type="application/json", **headers)


class HttpsRedirectTests(TestCase):
    @override_settings(SECURE_SSL_REDIRECT=True, DEVICE_API_TOKEN=TOKEN)
    def test_plain_http_is_redirected_to_https_in_production(self):
        res = self.client.get(reverse("assistant:health"))
        self.assertEqual(res.status_code, 301)
        self.assertTrue(res["Location"].startswith("https://"))
        self.assertEqual(self.client.get(reverse("assistant:health"), secure=True).status_code,
                         200)


class HealthTests(ApiTestCase):
    def test_health_is_public(self):
        res = self.client.get(reverse("assistant:health"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ok", "database": "ok"})

    def test_health_rejects_post(self):
        self.assertEqual(self.client.post(reverse("assistant:health")).status_code, 405)


class ChatApiTests(ApiTestCase):
    def test_successful_chat_creates_record(self):
        self.fake.add(
            response(function_call("create_item", {
                "type": "TASK", "title": "Sună la dentist", "description": None,
                "date": "2026-09-23", "time": "10:00"})),
            response(message("Am salvat taskul: Sună la dentist, mâine la ora 10.")),
        )
        res = self.post_chat({"message": "Amintește-mi mâine la 10 să sun la dentist."})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {
            "reply": "Am salvat taskul: Sună la dentist, mâine la ora 10.",
            "status": "success",
        })
        self.assertEqual(AgendaItem.objects.get().title, "Sună la dentist")

    def test_device_session_keeps_context_between_requests(self):
        self.fake.add(response(message("r1")), response(message("r2")))
        self.post_chat({"message": "first"})
        self.post_chat({"message": "second"})
        second_input = self.fake.calls[1]["input"]
        self.assertEqual(second_input[0], {"role": "user", "content": "first"})

    def test_x_device_token_header_accepted(self):
        self.fake.add(response(message("ok")))
        res = self.client.post(reverse("assistant:chat"), data=json.dumps({"message": "hi"}),
                               content_type="application/json", HTTP_X_DEVICE_TOKEN=TOKEN)
        self.assertEqual(res.status_code, 200)

    def test_missing_token(self):
        res = self.post_chat({"message": "hi"}, token=None)
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["status"], "error")
        self.assertEqual(self.fake.calls, [])

    def test_invalid_token(self):
        res = self.post_chat({"message": "hi"}, token="wrong")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(self.fake.calls, [])

    @override_settings(DEVICE_API_TOKEN="")
    def test_unconfigured_token_rejects_everything(self):
        res = self.post_chat({"message": "hi"}, token="")
        self.assertEqual(res.status_code, 503)
        res = self.post_chat({"message": "hi"}, token="anything")
        self.assertEqual(res.status_code, 503)

    def test_invalid_payloads(self):
        for body in ("{not json", "[]", json.dumps({"msg": "x"}), json.dumps({"message": ""}),
                     json.dumps({"message": "   "}), json.dumps({"message": 5}),
                     json.dumps({"message": "x" * 1001})):
            res = self.post_chat(body, raw=True)
            self.assertEqual(res.status_code, 400, body[:30])
            self.assertEqual(res.json()["status"], "error")
        self.assertEqual(self.fake.calls, [])

    def test_get_not_allowed(self):
        res = self.client.get(reverse("assistant:chat"), HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(res.status_code, 405)

    def test_ai_failure_returns_generic_error(self):
        from assistant.services.openai_client import AssistantUnavailableError
        self.fake.add(AssistantUnavailableError("The AI service is temporarily unavailable."))
        res = self.post_chat({"message": "What do I have today?"})
        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.json()["status"], "error")
        self.assertNotIn("Traceback", res.content.decode())

    def test_unexpected_exception_hides_details(self):
        with mock.patch.object(Orchestrator, "handle_message",
                               side_effect=RuntimeError("secret internals")):
            res = self.post_chat({"message": "hi"})
        self.assertEqual(res.status_code, 500)
        self.assertNotIn("secret internals", res.content.decode())


class AgendaTodayApiTests(ApiTestCase):
    def get_today(self, token=TOKEN):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        with mock.patch("assistant.views.now_local",
                        return_value=FIXED_NOW.astimezone(LONDON)):
            return self.client.get(reverse("assistant:agenda-today"), **headers)

    def test_returns_only_todays_active_items_from_database(self):
        today = agenda.create_item(type="APPOINTMENT", title="Dentist",
                                   starts_at=aware(2026, 9, 22, 15, 0))
        agenda.create_item(type="TASK", title="Tomorrow", due_at=aware(2026, 9, 23, 10, 0))
        done = agenda.create_item(type="TASK", title="Done", due_at=aware(2026, 9, 22, 8, 0))
        agenda.complete_item(done.id)
        undated = agenda.create_item(type="TASK", title="Someday")

        res = self.get_today()
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["date"], "2026-09-22")
        self.assertEqual([i["id"] for i in data["items"]], [str(today.id)])
        self.assertEqual(data["items"][0]["time"], "15:00")
        self.assertEqual([i["id"] for i in data["undated_tasks"]], [str(undated.id)])

    def test_requires_token(self):
        self.assertEqual(self.get_today(token=None).status_code, 401)
        self.assertEqual(self.get_today(token="bad").status_code, 401)


class ApiHardeningTests(ApiTestCase):
    def test_oversized_body_returns_413(self):
        res = self.post_chat(json.dumps({"message": "x" * 70_000}), raw=True)
        self.assertEqual(res.status_code, 413)
        self.assertEqual(res.json()["status"], "error")

    def test_concurrent_chat_returns_429_instead_of_queueing(self):
        DEVICE_SESSION_LOCK.acquire()
        try:
            with mock.patch.object(views, "CHAT_LOCK_TIMEOUT_SECONDS", 0.01):
                res = self.post_chat({"message": "hi"})
        finally:
            DEVICE_SESSION_LOCK.release()
        self.assertEqual(res.status_code, 429)
        self.assertEqual(self.fake.calls, [])

    def test_health_and_agenda_not_blocked_by_chat_lock(self):
        DEVICE_SESSION_LOCK.acquire()
        try:
            self.assertEqual(self.client.get(reverse("assistant:health")).status_code, 200)
            res = self.client.get(reverse("assistant:agenda-today"),
                                  HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
            self.assertEqual(res.status_code, 200)
        finally:
            DEVICE_SESSION_LOCK.release()

    def test_database_failure_on_agenda_returns_generic_500(self):
        with mock.patch.object(agenda, "list_items", side_effect=DatabaseError("db at C:/x")):
            res = self.client.get(reverse("assistant:agenda-today"),
                                  HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(res.status_code, 500)
        self.assertNotIn("C:/x", res.content.decode())

    def test_database_failure_during_chat_is_not_reported_as_success(self):
        def check(instructions, input_items):
            self.assertEqual(json.loads(input_items[-1]["output"])["error"], "database_error")
            return response(message("Nu am putut salva."))

        self.fake.add(response(function_call("create_item", {
            "type": "TASK", "title": "x", "description": None, "date": None, "time": None})),
            check)
        with mock.patch.object(agenda, "create_item", side_effect=DatabaseError("locked")):
            res = self.post_chat({"message": "Adaugă task x"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(AgendaItem.objects.count(), 0)

    def test_api_key_never_in_response_or_logs(self):
        secret = "sk-test-SECRET-value-123"
        client = OpenAIResponsesClient(api_key=secret, model="gpt-5.6-luna")
        client._client = mock.MagicMock()
        req = httpx.Request("POST", "https://api.openai.com/v1/responses")
        client._client.responses.create.side_effect = openai.AuthenticationError(
            f"Incorrect API key provided: {secret}",
            response=httpx.Response(401, request=req), body=None)
        with mock.patch.object(views, "get_orchestrator",
                               return_value=Orchestrator(client=client, clock=fixed_clock())):
            with self.assertLogs("assistant", level="DEBUG") as logs:
                res = self.post_chat({"message": "hi"})
        self.assertEqual(res.status_code, 502)
        self.assertNotIn(secret, res.content.decode())
        self.assertNotIn(secret, "\n".join(logs.output))
