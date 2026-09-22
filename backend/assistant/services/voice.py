"""
Voice pipeline: recording -> transcription -> existing orchestrator -> speech.

Idempotency (device retries must never repeat an agenda operation):

- Every request carries a device-generated ``request_id`` (UUID). It is the
  primary key of ``VoiceRequest``, so SQLite enforces uniqueness: only the
  request that inserts the row executes it.
- The row stores the SHA-256 of the recording; reusing an id with different
  audio is rejected.
- Progress is persisted in ``stage``. Only requests that never reached
  EXECUTING (nothing touched the agenda) may be re-run. A request interrupted
  during EXECUTING is never re-run; it is reported as interrupted.
- The orchestrator outcome is committed before speech is generated, and audio
  can be regenerated from the stored reply text without touching the agenda.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

from django.conf import settings
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from assistant.models import AgendaItem, VoiceRequest

from . import agenda
from .datetime_utils import MONTHS_RO, WEEKDAYS_EN, WEEKDAYS_RO, now_local, to_local
from .openai_client import AssistantError
from .orchestrator import Orchestrator
from .session import DEVICE_SESSION_LOCK, get_device_session
from .speech import SpeechSynthesizer
from .tools import MUTATING_TOOLS
from .transcription import AudioValidationError, Transcriber, validate_wav

logger = logging.getLogger("assistant.voice")

# A PROCESSING request not updated for this long is considered interrupted.
STALE_PROCESSING_SECONDS = 120
LOCK_TIMEOUT_SECONDS = 1.0

MESSAGES = {
    "ro": {
        "no_speech": "Nu te-am auzit. Ține apăsat butonul și încearcă din nou.",
        "interrupted": "Cererea anterioară a fost întreruptă. Verifică agenda înainte să o repeți.",
        "transcription_failed": "Nu am putut înțelege înregistrarea. Încearcă din nou.",
        "busy": "Procesez deja o cerere. Încearcă din nou imediat.",
    },
    "en": {
        "no_speech": "I didn't hear anything. Hold the button and try again.",
        "interrupted": "The previous request was interrupted. Check the agenda before repeating it.",
        "transcription_failed": "I couldn't understand the recording. Please try again.",
        "busy": "I'm already working on a request. Try again in a moment.",
    },
}


class RequestConflict(Exception):
    """The request_id was already used for a different payload."""


@dataclass
class VoiceResponse:
    http_status: int
    payload: dict[str, Any] = field(default_factory=dict)


def payload_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_request_id(value) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("request_id must be a UUID.") from exc


def result_payload(rec: VoiceRequest) -> dict[str, Any]:
    if rec.status == VoiceRequest.Status.PROCESSING:
        status = "processing"
    elif rec.status == VoiceRequest.Status.COMPLETED:
        status = rec.outcome or "success"
    else:
        status = "error"
    has_reply = bool(rec.reply_text) and rec.status != VoiceRequest.Status.PROCESSING
    return {
        "request_id": str(rec.id),
        "status": status,
        "stage": rec.stage,
        "transcript": rec.transcript,
        "reply": rec.reply_text,
        "error": rec.error_code or None,
        "retryable": rec.status == VoiceRequest.Status.FAILED and rec.is_safe_to_retry,
        "audio_url": f"/api/voice/audio/{rec.id}/" if has_reply else None,
        "audio_ready": rec.has_audio(),
    }


def purge_expired(now=None) -> None:
    """Keep temporary storage bounded: drop expired audio and old request records."""
    now = now or timezone.now()
    VoiceRequest.objects.filter(audio_expires_at__lte=now).exclude(audio=None).update(
        audio=None, audio_expires_at=None)
    cutoff = now - timedelta(hours=settings.VOICE_REQUEST_RETENTION_HOURS)
    VoiceRequest.objects.filter(created_at__lt=cutoff).exclude(
        status=VoiceRequest.Status.PROCESSING).delete()


def build_agenda_summary(day: date, language: str = "ro") -> str:
    """Deterministic spoken summary of a day's active agenda (read-only)."""
    items = agenda.list_items(date_from=day, date_to=day)
    undated = list(AgendaItem.objects.active().undated().order_by("created_at")[:4])

    def when(item) -> str | None:
        has_time = item.type == AgendaItem.Type.APPOINTMENT or item.due_has_time
        return to_local(item.when).strftime("%H:%M") if has_time else None

    if language == "en":
        head = f"Today, {WEEKDAYS_EN[day.weekday()]} {day.day} {day.strftime('%B')}"
        parts = []
        for i in items:
            kind = "appointment" if i.type == AgendaItem.Type.APPOINTMENT else "task"
            t = when(i)
            parts.append(f"{'at ' + t if t else 'any time'}, {kind}: {i.title}")
        text = (f"{head}, you have {len(items)} item{'s' if len(items) != 1 else ''}: "
                + "; ".join(parts) + ".") if items else f"{head}, your agenda is empty."
        if undated:
            text += f" Tasks without a date: {', '.join(i.title for i in undated[:3])}."
        return text

    head = f"Astăzi, {WEEKDAYS_RO[day.weekday()]} {day.day} {MONTHS_RO[day.month - 1]}"
    parts = []
    for i in items:
        kind = "programare" if i.type == AgendaItem.Type.APPOINTMENT else "task"
        t = when(i)
        parts.append(f"{'la ' + t if t else 'fără oră'}, {kind}: {i.title}")
    if items:
        count = "un lucru" if len(items) == 1 else f"{len(items)} lucruri"
        text = f"{head}, ai {count} în agendă: " + "; ".join(parts) + "."
    else:
        text = f"{head}, nu ai nimic în agendă."
    if undated:
        text += f" Taskuri fără dată: {', '.join(i.title for i in undated[:3])}."
    return text


class VoiceService:
    def __init__(self, transcriber: Transcriber | None = None,
                 synthesizer: SpeechSynthesizer | None = None,
                 orchestrator: Orchestrator | None = None,
                 session_getter: Callable = get_device_session,
                 lock: threading.Lock = DEVICE_SESSION_LOCK,
                 clock=None):
        self.transcriber = transcriber or Transcriber()
        self.synthesizer = synthesizer or SpeechSynthesizer()
        self._orchestrator = orchestrator
        self.session_getter = session_getter
        self.lock = lock
        self.clock = clock

    @property
    def orchestrator(self) -> Orchestrator:
        if self._orchestrator is None:
            self._orchestrator = Orchestrator(clock=self.clock)
        return self._orchestrator

    # --- idempotency -----------------------------------------------------------

    def claim(self, request_id: uuid.UUID, kind: str, digest: str) -> tuple[VoiceRequest, bool]:
        """Return (record, claimed). Only a claimed record may be executed by the caller."""
        try:
            with transaction.atomic():
                rec = VoiceRequest.objects.create(id=request_id, kind=kind, payload_sha256=digest)
            return rec, True
        except IntegrityError:
            pass

        rec = VoiceRequest.objects.get(pk=request_id)
        if rec.payload_sha256 != digest or rec.kind != kind:
            raise RequestConflict("request_id was already used for a different request.")

        if rec.status == VoiceRequest.Status.FAILED and rec.is_safe_to_retry:
            return rec, self._reclaim(rec, VoiceRequest.Status.FAILED)

        if rec.status == VoiceRequest.Status.PROCESSING:
            stale = rec.updated_at < timezone.now() - timedelta(seconds=STALE_PROCESSING_SECONDS)
            if stale and rec.is_safe_to_retry:
                return rec, self._reclaim(rec, VoiceRequest.Status.PROCESSING)
            if stale:
                # Interrupted while the agenda may have been changing: never re-run.
                updated = VoiceRequest.objects.filter(
                    pk=rec.pk, status=VoiceRequest.Status.PROCESSING,
                    updated_at=rec.updated_at,
                ).update(status=VoiceRequest.Status.FAILED, outcome="error",
                         error_code="interrupted",
                         reply_text=MESSAGES["ro"]["interrupted"], updated_at=timezone.now())
                if updated:
                    logger.warning("Voice request %s interrupted during execution", rec.pk)
                rec.refresh_from_db()
        return rec, False

    @staticmethod
    def _reclaim(rec: VoiceRequest, from_status: str) -> bool:
        """Atomically move a retryable record back to PROCESSING (only one caller wins)."""
        won = VoiceRequest.objects.filter(
            pk=rec.pk, status=from_status, stage__in=VoiceRequest.SAFE_TO_RETRY_STAGES,
            updated_at=rec.updated_at,
        ).update(status=VoiceRequest.Status.PROCESSING, stage=VoiceRequest.Stage.RECEIVED,
                 error_code="", outcome="", reply_text="", updated_at=timezone.now())
        rec.refresh_from_db()
        return bool(won)

    @staticmethod
    def _save(rec: VoiceRequest, **fields) -> None:
        for name, value in fields.items():
            setattr(rec, name, value)
        rec.save(update_fields=[*fields, "updated_at"])

    # --- voice command -----------------------------------------------------------

    def handle_voice(self, request_id, audio: bytes) -> VoiceResponse:
        purge_expired()
        try:
            rid = parse_request_id(request_id)
        except ValueError as exc:
            return VoiceResponse(400, {"status": "error", "error": "invalid_request_id",
                                       "reply": str(exc)})
        try:
            validate_wav(audio)
        except AudioValidationError as exc:
            status = 413 if exc.code == "audio_too_large" else (
                415 if exc.code == "unsupported_format" else 400)
            return VoiceResponse(status, {"request_id": str(rid), "status": "error",
                                          "error": exc.code, "reply": str(exc)})

        if not self.lock.acquire(timeout=LOCK_TIMEOUT_SECONDS):
            return VoiceResponse(429, {"request_id": str(rid), "status": "busy",
                                       "error": "busy", "reply": MESSAGES["ro"]["busy"]})
        try:
            try:
                rec, claimed = self.claim(rid, VoiceRequest.Kind.VOICE, payload_digest(audio))
            except RequestConflict as exc:
                return VoiceResponse(409, {"request_id": str(rid), "status": "error",
                                           "error": "request_id_conflict", "reply": str(exc)})
            except OperationalError:
                # Database busy (e.g. a concurrent duplicate from another process). Nothing
                # was executed for this call, so the device can safely retry the same id.
                logger.warning("Database busy while claiming voice request %s", rid)
                return VoiceResponse(429, {"request_id": str(rid), "status": "busy",
                                           "error": "busy", "reply": MESSAGES["ro"]["busy"]})
            if not claimed:
                return self._existing(rec)
            self._execute_voice(rec, audio)
        finally:
            self.lock.release()

        self._ensure_audio(rec)
        return VoiceResponse(502 if rec.status == VoiceRequest.Status.FAILED else 200,
                             result_payload(rec))

    def _execute_voice(self, rec: VoiceRequest, audio: bytes) -> None:
        # 1. Transcription: nothing has touched the agenda yet, so failures are retryable.
        try:
            transcript = self.transcriber.transcribe(audio)
        except AudioValidationError as exc:
            self._save(rec, status=VoiceRequest.Status.FAILED, error_code=exc.code,
                       reply_text=str(exc))
            return
        except AssistantError:
            self._save(rec, status=VoiceRequest.Status.FAILED,
                       error_code="transcription_failed",
                       reply_text=MESSAGES["ro"]["transcription_failed"])
            return

        if not transcript:
            self._save(rec, status=VoiceRequest.Status.COMPLETED, outcome="no_speech",
                       stage=VoiceRequest.Stage.EXECUTED, reply_text=MESSAGES["ro"]["no_speech"])
            return
        self._save(rec, transcript=transcript, stage=VoiceRequest.Stage.TRANSCRIBED)

        # 2. Execution: from here on the agenda may change, so the request is never re-run.
        self._save(rec, stage=VoiceRequest.Stage.EXECUTING)
        try:
            reply = self.orchestrator.handle_message(self.session_getter(), transcript)
        except Exception:
            logger.exception("Orchestrator crashed for voice request %s", rec.pk)
            self._save(rec, status=VoiceRequest.Status.FAILED, outcome="error",
                       error_code="interrupted", reply_text=MESSAGES["ro"]["interrupted"])
            return

        mutated = any(a["ok"] and a["tool"] in MUTATING_TOOLS for a in reply.actions)
        if not reply.ok and not mutated:
            # e.g. OpenAI unavailable before any agenda change: safe to retry later.
            self._save(rec, status=VoiceRequest.Status.FAILED, outcome="error",
                       error_code="assistant_failed", reply_text=reply.text,
                       actions=reply.actions, stage=VoiceRequest.Stage.TRANSCRIBED)
            return
        # 3. Persist the real outcome BEFORE any speech is generated.
        self._save(rec, status=VoiceRequest.Status.COMPLETED,
                   outcome="success" if reply.ok else "error",
                   error_code="" if reply.ok else "assistant_failed",
                   reply_text=reply.text, actions=reply.actions,
                   stage=VoiceRequest.Stage.EXECUTED)

    def _existing(self, rec: VoiceRequest) -> VoiceResponse:
        if rec.status == VoiceRequest.Status.PROCESSING:
            return VoiceResponse(202, result_payload(rec))
        self._ensure_audio(rec)
        return VoiceResponse(200 if rec.status == VoiceRequest.Status.COMPLETED else 502,
                             result_payload(rec))

    # --- Button B: today's agenda ----------------------------------------------------

    def handle_agenda_summary(self, request_id, language: str = "ro") -> VoiceResponse:
        purge_expired()
        try:
            rid = parse_request_id(request_id)
        except ValueError as exc:
            return VoiceResponse(400, {"status": "error", "error": "invalid_request_id",
                                       "reply": str(exc)})
        language = "en" if language == "en" else "ro"
        today = now_local(self.clock).date()
        digest = payload_digest(f"agenda-summary:{language}:{today.isoformat()}".encode())
        try:
            rec, claimed = self.claim(rid, VoiceRequest.Kind.AGENDA_SUMMARY, digest)
        except RequestConflict as exc:
            return VoiceResponse(409, {"request_id": str(rid), "status": "error",
                                       "error": "request_id_conflict", "reply": str(exc)})
        if not claimed:
            return self._existing(rec)
        # Read-only: no agenda mutation can happen here.
        self._save(rec, status=VoiceRequest.Status.COMPLETED, outcome="success",
                   stage=VoiceRequest.Stage.EXECUTED, language=language,
                   reply_text=build_agenda_summary(today, language))
        self._ensure_audio(rec)
        return VoiceResponse(200, result_payload(rec))

    # --- audio -----------------------------------------------------------------------

    def _ensure_audio(self, rec: VoiceRequest) -> bool:
        """Generate reply audio if missing. Never re-runs the agenda operation."""
        if rec.has_audio() or not rec.reply_text:
            return rec.has_audio()
        try:
            wav = self.synthesizer.synthesize(rec.reply_text)
        except (AssistantError, ValueError):
            logger.warning("Speech generation failed for request %s", rec.pk)
            return False
        self._save(rec, audio=wav, audio_expires_at=timezone.now()
                   + timedelta(seconds=settings.VOICE_AUDIO_TTL_SECONDS))
        return True

    def get_audio(self, request_id) -> tuple[int, bytes | None, dict]:
        """Return (http_status, wav_bytes, error_payload)."""
        purge_expired()
        try:
            rid = parse_request_id(request_id)
        except ValueError:
            return 400, None, {"status": "error", "error": "invalid_request_id"}
        rec = VoiceRequest.objects.filter(pk=rid).first()
        if rec is None:
            return 404, None, {"status": "error", "error": "not_found"}
        if rec.status == VoiceRequest.Status.PROCESSING:
            return 409, None, {"status": "processing", "error": "not_ready"}
        if not rec.reply_text:
            return 404, None, {"status": "error", "error": "no_reply"}
        if not self._ensure_audio(rec):
            return 502, None, {"status": "error", "error": "tts_failed"}
        return 200, bytes(rec.audio), {}

    def get_status(self, request_id) -> VoiceResponse:
        try:
            rid = parse_request_id(request_id)
        except ValueError as exc:
            return VoiceResponse(400, {"status": "error", "error": "invalid_request_id",
                                       "reply": str(exc)})
        rec = VoiceRequest.objects.filter(pk=rid).first()
        if rec is None:
            return VoiceResponse(404, {"request_id": str(rid), "status": "unknown",
                                       "error": "not_found"})
        # Surface interrupted executions instead of leaving them "processing" forever.
        if rec.status == VoiceRequest.Status.PROCESSING:
            stale = rec.updated_at < timezone.now() - timedelta(seconds=STALE_PROCESSING_SECONDS)
            if stale and not rec.is_safe_to_retry:
                rec, _ = self.claim(rid, rec.kind, rec.payload_sha256)
        return VoiceResponse(200, result_payload(rec))
