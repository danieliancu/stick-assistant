"""
Live end-to-end voice check against the real OpenAI API (incurs API charges).

Uploads real speech fixtures through the actual HTTP voice endpoints
(multipart + device token), which run transcription -> existing orchestrator ->
agenda service -> TTS. SQLite is verified independently after every step, the
returned WAV audio is validated, and only records created by this run are
removed afterwards. Never run automatically in CI.
"""

import io
import sys
import uuid
import wave
from datetime import time, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import Client

from assistant.models import AgendaItem, VoiceRequest
from assistant.services.datetime_utils import make_local_aware, now_local, to_local
from assistant.services.transcription import validate_wav

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "voice"


class Command(BaseCommand):
    help = "Run a live voice pipeline check (real OpenAI STT, orchestrator and TTS)."

    def handle(self, *args, **options):
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if not settings.OPENAI_API_KEY:
            raise CommandError("OPENAI_API_KEY is not configured; voice check NOT VERIFIED.")
        if not settings.DEVICE_API_TOKEN:
            raise CommandError("DEVICE_API_TOKEN is not configured.")

        self.client = Client(HTTP_HOST="localhost",
                             HTTP_AUTHORIZATION=f"Bearer {settings.DEVICE_API_TOKEN}")
        self.preexisting = set(AgendaItem.objects.values_list("pk", flat=True))
        self.request_ids: list[str] = []
        self.results: list[tuple[str, bool]] = []
        tomorrow = now_local().date() + timedelta(days=1)
        self.stdout.write(f"STT: {settings.OPENAI_TRANSCRIBE_MODEL} | TTS: "
                          f"{settings.OPENAI_TTS_MODEL} ({settings.OPENAI_TTS_VOICE}) | "
                          f"LLM: {settings.OPENAI_MODEL}")
        try:
            # 1. Create a task by voice.
            rid, body = self.upload("ro_create_task")
            new = self.new_items()
            task = new[0] if len(new) == 1 else None
            self.verify("Voice create task (SQLite)", task is not None and task.type == "TASK"
                        and "dentist" in task.title.lower()
                        and task.due_at == make_local_aware(tomorrow, time(10, 0)),
                        self.describe(new))
            self.verify("Reply audio is valid WAV", self.check_audio(body))

            # 2. Device retry with the same request_id must not duplicate the task.
            _, retry = self.upload("ro_create_task", request_id=rid)
            self.verify("Retry same request_id: same outcome, no duplicate",
                        retry.get("reply") == body.get("reply") and len(self.new_items()) == 1,
                        f"{len(self.new_items())} new item(s)")

            # 3. Same request_id with different audio is rejected.
            status, _ = self.upload("ro_no", request_id=rid, expect_json=True, raw_status=True)
            self.verify("Same request_id + different audio -> 409", status == 409, str(status))

            # 4. Query.
            _, body = self.upload("ro_query_tomorrow")
            self.verify("Voice query returns saved task", "dentist" in body["reply"].lower(),
                        body["reply"])

            # 5. Follow-up reference.
            self.upload("ro_move_to_noon")
            if task:
                task.refresh_from_db()
            self.verify("Voice follow-up moves the same record", task is not None
                        and task.due_at == make_local_aware(tomorrow, time(12, 0))
                        and len(self.new_items()) == 1,
                        self.describe([task] if task else []))

            # 6. Button B: today's agenda summary.
            summary_id = str(uuid.uuid4())
            self.request_ids.append(summary_id)
            res = self.client.post("/api/voice/agenda/", {"request_id": summary_id,
                                                          "language": "ro"},
                                   content_type="application/json", secure=True)
            summary = res.json()
            self.stdout.write(f"\n[Button B] Assistant: {summary.get('reply')}")
            self.verify("Agenda summary + audio", res.status_code == 200
                        and self.check_audio(summary))

            # 7. Delete request, then a spoken "Nu".
            _, body = self.upload("ro_delete_task")
            exists = task is not None and AgendaItem.objects.filter(pk=task.pk).exists()
            self.verify("Delete request does not delete without confirmation", exists,
                        body["reply"])
            _, body = self.upload("ro_no")
            exists = task is not None and AgendaItem.objects.filter(pk=task.pk).exists()
            self.verify("Spoken 'Nu' keeps the record", exists, body["reply"])

            # 8. English.
            _, body = self.upload("en_query_tomorrow")
            self.verify("English voice query", "dentist" in body["reply"].lower(), body["reply"])
        finally:
            self.cleanup()

        passed = sum(1 for _, ok in self.results if ok)
        self.stdout.write(f"\nVOICE CHECK: {passed}/{len(self.results)} checks passed")
        if passed != len(self.results):
            raise CommandError("Voice check failed.")

    # --- helpers ---------------------------------------------------------------------

    def upload(self, fixture, request_id=None, expect_json=True, raw_status=False):
        request_id = request_id or str(uuid.uuid4())
        if request_id not in self.request_ids:
            self.request_ids.append(request_id)
        audio = (FIXTURES / f"{fixture}.wav").read_bytes()
        validate_wav(audio)
        upload = io.BytesIO(audio)
        upload.name = f"{fixture}.wav"
        res = self.client.post("/api/voice/", {"request_id": request_id, "audio": upload},
                               secure=True)
        body = res.json()
        if raw_status:
            return res.status_code, body
        self.stdout.write(f"\n[{fixture}] HTTP {res.status_code} status={body.get('status')}\n"
                          f"  heard:     {body.get('transcript')}\n"
                          f"  assistant: {body.get('reply')}")
        return request_id, body

    def check_audio(self, body) -> bool:
        if not body.get("audio_url"):
            return False
        res = self.client.get(body["audio_url"], secure=True)
        if res.status_code != 200 or res["Content-Type"] != "audio/wav":
            return False
        with wave.open(io.BytesIO(res.content)) as w:
            ok = w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getnframes() > 0
            self.stdout.write(f"  audio: {w.getframerate()} Hz, "
                              f"{w.getnframes() / w.getframerate():.2f}s, {len(res.content)} bytes")
        return ok

    def new_items(self):
        return list(AgendaItem.objects.exclude(pk__in=self.preexisting).order_by("created_at"))

    @staticmethod
    def describe(items):
        return "; ".join(
            f"{i.type} '{i.title}' {to_local(i.when).strftime('%Y-%m-%d %H:%M') if i.when else '-'}"
            f" {i.status}" for i in items) or "no new items"

    def verify(self, name, ok, detail=""):
        self.results.append((name, ok))
        style = self.style.SUCCESS if ok else self.style.ERROR
        self.stdout.write(style(f"  [{'PASS' if ok else 'FAIL'}] {name}")
                          + (f" — {detail}" if detail else ""))

    def cleanup(self):
        created = AgendaItem.objects.exclude(pk__in=self.preexisting)
        count = created.count()
        created.delete()
        requests = VoiceRequest.objects.filter(pk__in=self.request_ids).delete()[0]
        untouched = AgendaItem.objects.filter(pk__in=self.preexisting).count()
        self.stdout.write(f"\nCleanup: removed {count} agenda record(s) and {requests} voice "
                          f"request(s) created by this run; {untouched} pre-existing "
                          "record(s) untouched.")
