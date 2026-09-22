"""
Live end-to-end check against the real OpenAI API (costs a few API calls).

Runs a scripted Romanian/English conversation through the real orchestrator,
verifies SQLite independently after every step, and removes only the records
this run created. Never used by the automated test suite or CI.
"""

import sys
from datetime import time, timedelta

from django.core.management.base import BaseCommand, CommandError

from assistant.models import AgendaItem
from assistant.services.datetime_utils import (
    make_local_aware,
    now_local,
    to_local,
    weekday_next_week,
    next_weekday,
)
from assistant.services.openai_client import OpenAIResponsesClient
from assistant.services.orchestrator import Orchestrator
from assistant.services.session import ConversationSession

MARKER = "LIVETEST"


class Command(BaseCommand):
    help = "Run a live end-to-end conversation against the real OpenAI API."

    def handle(self, *args, **options):
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        client = OpenAIResponsesClient()
        if not client.api_key:
            raise CommandError("OPENAI_API_KEY is not configured; live check NOT VERIFIED.")

        self.orchestrator = Orchestrator(client=client)
        self.session = ConversationSession()
        self.preexisting = set(AgendaItem.objects.values_list("pk", flat=True))
        self.results: list[tuple[str, bool, str]] = []
        today = now_local().date()
        tomorrow = today + timedelta(days=1)

        self.stdout.write(f"Model: {client.model} | reasoning effort: {client.reasoning_effort}")
        try:
            # 1. Create a task in Romanian.
            self.say(f"Amintește-mi mâine la 10 să sun la dentist ({MARKER}).")
            new = self.new_items()
            task = new[0] if len(new) == 1 else None
            self.verify("RO create task", task is not None and task.type == "TASK"
                       and task.due_at == make_local_aware(tomorrow, time(10, 0)),
                       self.describe(new))

            # 2. Retrieve it.
            reply = self.say("Ce am de făcut mâine?")
            self.verify("RO retrieve task", self.used("list_items")
                       and "dentist" in reply.lower(), reply)

            # 3. Follow-up time change.
            self.say("Mută-l la ora 12.")
            if task:
                task.refresh_from_db()
            self.verify("RO follow-up move (same record)", task is not None
                       and task.due_at == make_local_aware(tomorrow, time(12, 0))
                       and len(self.new_items()) == 1, self.describe([task] if task else []))

            # 4. Complete it.
            self.say("Marchează-l ca finalizat.")
            if task:
                task.refresh_from_db()
            self.verify("RO complete task", task is not None
                       and task.status == AgendaItem.Status.COMPLETED,
                       self.describe([task] if task else []))

            # 5. Retrieve completed tasks.
            reply = self.say("Ce taskuri am finalizat?")
            self.verify("RO list completed", self.used("list_items")
                       and "dentist" in reply.lower(), reply)

            # 6. Appointment without a time must not be saved.
            before = len(self.new_items())
            reply = self.say(f"Am programare la frizer ({MARKER}) vineri.")
            self.verify("RO appointment without time asks, saves nothing",
                       len(self.new_items()) == before and "?" in reply, reply)
            self.say("Nu contează, las-o baltă.")

            # 7. Create an appointment in English.
            self.say(f"Add an appointment: optician ({MARKER}) next Friday at 14:30.")
            appts = [i for i in self.new_items() if i.type == "APPOINTMENT"]
            appt = appts[0] if len(appts) == 1 else None
            fridays = {next_weekday(today, 4), weekday_next_week(today, 4)}
            ok = appt is not None and to_local(appt.starts_at).time() == time(14, 30) \
                and to_local(appt.starts_at).date() in fridays
            self.verify("EN create appointment", ok, self.describe(appts))

            # 8. Retrieve it in Romanian.
            reply = self.say("Ce programări am în următoarele două săptămâni?")
            self.verify("RO retrieve appointment", self.used("list_items")
                       and "optician" in reply.lower(), reply)

            # 9. Delete: first request must not delete.
            self.say("Șterge programarea la optician.")
            still_there = appt is not None and AgendaItem.objects.filter(pk=appt.pk).exists()
            self.verify("Delete asks for confirmation first", still_there, self.last_reply)

            # 10. Confirm.
            self.say("Da, confirm.")
            gone = appt is not None and not AgendaItem.objects.filter(pk=appt.pk).exists()
            self.verify("Delete after confirmation", gone, self.last_reply)
        finally:
            self.cleanup()

        passed = sum(1 for _, ok, _ in self.results if ok)
        self.stdout.write(f"\nLIVE CHECK: {passed}/{len(self.results)} checks passed")
        if passed != len(self.results):
            raise CommandError("Live check failed.")

    # --- helpers -------------------------------------------------------------

    def say(self, text):
        reply = self.orchestrator.handle_message(self.session, text)
        self.last_actions = reply.actions
        self.last_reply = reply.text
        tools = ", ".join(f"{a['tool']}({'ok' if a['ok'] else a['error']})" for a in reply.actions)
        self.stdout.write(f"\nYou: {text}\nAssistant [{reply.status}]: {reply.text}\n"
                          f"  tools: {tools or '-'}")
        return reply.text

    def used(self, tool):
        return any(a["tool"] == tool and a["ok"] for a in self.last_actions)

    def new_items(self):
        return list(AgendaItem.objects.exclude(pk__in=self.preexisting).order_by("created_at"))

    @staticmethod
    def describe(items):
        return "; ".join(
            f"{i.type} '{i.title}' {to_local(i.when).strftime('%Y-%m-%d %H:%M') if i.when else '-'}"
            f" {i.status}" for i in items) or "no new items"

    def verify(self, name, ok, detail):
        self.results.append((name, ok, detail))
        style = self.style.SUCCESS if ok else self.style.ERROR
        self.stdout.write(style(f"  [{'PASS' if ok else 'FAIL'}] {name}") + f" — DB/reply: {detail}")

    def cleanup(self):
        created = AgendaItem.objects.exclude(pk__in=self.preexisting)
        count = created.count()
        created.delete()
        untouched = AgendaItem.objects.filter(pk__in=self.preexisting).count()
        self.stdout.write(f"\nCleanup: removed {count} record(s) created by this run; "
                          f"{untouched} pre-existing record(s) untouched.")
