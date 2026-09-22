"""
Minimal JSON API for the future M5StickS3 firmware.

Business logic lives in ``assistant.services``; views only handle HTTP,
authentication and payload validation.
"""

import hmac
import json
import logging
from functools import wraps

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.db import DatabaseError, connection
from django.http import HttpResponse, JsonResponse
from django.http.multipartparser import MultiPartParserError
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import AgendaItem
from .services import agenda
from .services.datetime_utils import now_local
from .services.orchestrator import MAX_MESSAGE_LENGTH, Orchestrator
from .services.session import DEVICE_SESSION_LOCK, get_device_session
from .services.transcription import max_upload_bytes
from .services.voice import VoiceService

logger = logging.getLogger("assistant.api")

# How long a chat request waits for another in-flight chat request to finish.
# A device retry while the first request is still running gets 429 instead of
# queueing up and possibly repeating the same operation.
CHAT_LOCK_TIMEOUT_SECONDS = 1.0


def _error(message: str, status: int) -> JsonResponse:
    return JsonResponse({"status": "error", "error": message}, status=status)


def _extract_token(request) -> str:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("X-Device-Token", "").strip()


def require_device_token(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        expected = settings.DEVICE_API_TOKEN
        if not expected:
            logger.error("DEVICE_API_TOKEN is not configured; rejecting API request")
            return _error("API is not configured.", 503)
        provided = _extract_token(request)
        if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
            return _error("Invalid or missing device token.", 401)
        return view(request, *args, **kwargs)

    return wrapper


def get_orchestrator() -> Orchestrator:
    """Factory, patched in tests."""
    return Orchestrator()


@require_GET
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        database = "ok"
    except DatabaseError:
        logger.exception("Health check database failure")
        database = "error"
    status = 200 if database == "ok" else 503
    return JsonResponse({"status": "ok" if status == 200 else "error", "database": database},
                        status=status)


@csrf_exempt
@require_POST
@require_device_token
def chat(request):
    try:
        body = request.body
    except RequestDataTooBig:
        return _error("Request body is too large.", 413)
    try:
        payload = json.loads(body.decode("utf-8") or "null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error("Request body must be valid JSON.", 400)
    if not isinstance(payload, dict):
        return _error("Request body must be a JSON object.", 400)
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        return _error("'message' must be a non-empty string.", 400)
    if len(message) > MAX_MESSAGE_LENGTH:
        return _error(f"'message' must be at most {MAX_MESSAGE_LENGTH} characters.", 400)

    if not DEVICE_SESSION_LOCK.acquire(timeout=CHAT_LOCK_TIMEOUT_SECONDS):
        return _error("Another request is still being processed. Try again shortly.", 429)
    try:
        reply = get_orchestrator().handle_message(get_device_session(), message)
    except Exception:
        logger.exception("Unexpected error in chat endpoint")
        return _error("Internal error.", 500)
    finally:
        DEVICE_SESSION_LOCK.release()

    if not reply.ok:
        return JsonResponse({"status": "error", "reply": reply.text}, status=502)
    return JsonResponse({"status": "success", "reply": reply.text})


@require_GET
@require_device_token
def agenda_today(request):
    today = now_local().date()
    try:
        dated = agenda.list_items(date_from=today, date_to=today)
        undated = list(AgendaItem.objects.active().undated().order_by("created_at"))
    except DatabaseError:
        logger.exception("Database error in agenda_today")
        return _error("Database error.", 500)
    return JsonResponse({
        "status": "success",
        "date": today.isoformat(),
        "items": [agenda.serialize_item(i) for i in dated],
        "undated_tasks": [agenda.serialize_item(i) for i in undated],
    })


# --- Voice (Stage 2) -------------------------------------------------------------

def get_voice_service() -> VoiceService:
    """Factory, patched in tests."""
    return VoiceService()


def _voice_response(result) -> JsonResponse:
    return JsonResponse(result.payload, status=result.http_status)


@csrf_exempt
@require_POST
@require_device_token
def voice(request):
    """multipart/form-data: audio=<WAV>, request_id=<UUID>."""
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        return _error("Content-Length is required.", 411)
    if length > max_upload_bytes() + 16 * 1024:
        return _error("The recording is too large.", 413)
    try:
        upload = request.FILES.get("audio")
        request_id = request.POST.get("request_id", "")
    except (MultiPartParserError, RequestDataTooBig):
        return _error("Invalid multipart body.", 400)
    if upload is None:
        return _error("'audio' file is required.", 400)
    if upload.size > max_upload_bytes():
        return _error("The recording is too large.", 413)
    audio = upload.read()
    try:
        result = get_voice_service().handle_voice(request_id, audio)
    except Exception:
        logger.exception("Unexpected error in voice endpoint")
        return _error("Internal error.", 500)
    return _voice_response(result)


@csrf_exempt
@require_POST
@require_device_token
def voice_agenda(request):
    """JSON: {"request_id": "<UUID>", "language": "ro"|"en"} -> spoken summary of today."""
    try:
        payload = json.loads(request.body.decode("utf-8") or "null")
    except (RequestDataTooBig, UnicodeDecodeError, json.JSONDecodeError):
        return _error("Request body must be valid JSON.", 400)
    if not isinstance(payload, dict):
        return _error("Request body must be a JSON object.", 400)
    try:
        result = get_voice_service().handle_agenda_summary(
            payload.get("request_id"), str(payload.get("language") or "ro"))
    except Exception:
        logger.exception("Unexpected error in voice agenda endpoint")
        return _error("Internal error.", 500)
    return _voice_response(result)


@require_GET
@require_device_token
def voice_request_status(request, request_id):
    return _voice_response(get_voice_service().get_status(request_id))


@require_GET
@require_device_token
def voice_audio(request, request_id):
    try:
        status, wav, error = get_voice_service().get_audio(request_id)
    except Exception:
        logger.exception("Unexpected error in voice audio endpoint")
        return _error("Internal error.", 500)
    if wav is None:
        return JsonResponse(error, status=status)
    response = HttpResponse(wav, content_type="audio/wav")
    response["Content-Length"] = str(len(wav))
    response["Cache-Control"] = "no-store"
    return response
