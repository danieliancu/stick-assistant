"""Stage 2 voice pipeline tests. OpenAI is always mocked; no network calls."""

import io
import json
import math
import threading
import time
import uuid
import wave
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

import httpx
import openai
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assistant import views
from assistant.models import AgendaItem, VoiceRequest
from assistant.services import agenda
from assistant.services.openai_client import AssistantConfigError, AssistantUnavailableError
from assistant.services.orchestrator import Orchestrator
from assistant.services.session import ConversationSession
from assistant.services.speech import (
    SpeechSynthesizer,
    pcm_to_wav,
    resample_pcm16,
    spoken_text,
)
from assistant.services.transcription import (
    AudioValidationError,
    Transcriber,
    max_upload_bytes,
    validate_wav,
)
from assistant.services.voice import (
    STALE_PROCESSING_SECONDS,
    VoiceService,
    build_agenda_summary,
    payload_digest,
)
from assistant.tests.helpers import (
    FIXED_NOW,
    FakeResponsesClient,
    fixed_clock,
    function_call,
    message,
    response,
    tool_outputs,
)

TOKEN = "voice-test-token"


def make_wav(seconds=1.0, rate=16000, amplitude=8000, channels=1, width=2, freq=440.0) -> bytes:
    frames = int(seconds * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        data = bytearray()
        for i in range(frames):
            value = int(amplitude * math.sin(2 * math.pi * freq * i / rate))
            sample = value.to_bytes(width, "little", signed=True) if width > 1 else \
                bytes([(value >> 8) + 128 & 0xFF])
            data += sample * channels
        wav.writeframes(bytes(data))
    return buffer.getvalue()


SPEECH = make_wav(1.0)
OTHER_SPEECH = make_wav(1.2, freq=660.0)
REPLY_WAV = pcm_to_wav(b"\x01\x00" * 2400, 24000)


class FakeTranscriber:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        validate_wav(audio)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class FakeSynth:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def synthesize(self, text):
        self.calls.append(text)
        if self.fail:
            raise AssistantUnavailableError("tts down")
        return REPLY_WAV


def create_task_call(title="Sună la dentist"):
    return function_call("create_item", {"type": "TASK", "title": title, "description": None,
                                         "date": "2026-09-23", "time": "10:00"})


class VoiceTestCase(TestCase):
    def setUp(self):
        self.fake = FakeResponsesClient()
        self.session = ConversationSession()
        self.synth = FakeSynth()
        self.transcriber = FakeTranscriber("Amintește-mi mâine la 10 să sun la dentist.")
        self.service = self.make_service()

    def make_service(self):
        return VoiceService(
            transcriber=self.transcriber, synthesizer=self.synth,
            orchestrator=Orchestrator(client=self.fake, clock=fixed_clock()),
            session_getter=lambda: self.session, lock=threading.Lock(), clock=fixed_clock())

    def script_create(self):
        self.fake.add(response(create_task_call()),
                      response(message("Am salvat taskul pentru mâine la ora 10.")))


# --- transcription -----------------------------------------------------------------

class WavValidationTests(TestCase):
    def assert_code(self, data, code):
        with self.assertRaises(AudioValidationError) as ctx:
            validate_wav(data)
        self.assertEqual(ctx.exception.code, code)

    def test_valid_wav(self):
        info = validate_wav(SPEECH)
        self.assertEqual((info.sample_rate, info.channels, info.sample_width), (16000, 1, 2))
        self.assertAlmostEqual(info.duration, 1.0, places=2)

    def test_empty(self):
        self.assert_code(b"", "empty_audio")
        self.assert_code(pcm_to_wav(b"", 16000), "empty_audio")

    def test_corrupted(self):
        self.assert_code(b"RIFF\x00\x00\x00\x00WAVEgarbage-garbage", "corrupted_audio")
        truncated = SPEECH[:-1000]  # header still claims the full data chunk
        self.assert_code(truncated, "corrupted_audio")

    def test_unsupported_formats(self):
        self.assert_code(b"ID3\x03\x00" + b"\x00" * 100, "unsupported_format")  # MP3
        self.assert_code(make_wav(1.0, channels=2), "unsupported_format")
        self.assert_code(make_wav(1.0, width=1), "unsupported_format")
        self.assert_code(make_wav(1.0, rate=12345), "unsupported_format")
        float_wav = bytearray(SPEECH)
        float_wav[20:22] = (3).to_bytes(2, "little")  # WAVE_FORMAT_IEEE_FLOAT
        self.assert_code(bytes(float_wav), "unsupported_format")

    def test_oversized(self):
        self.assert_code(b"RIFF" + b"\x00" * (max_upload_bytes() + 1), "audio_too_large")

    def test_duration_limits(self):
        self.assert_code(make_wav(13.5, rate=8000), "audio_too_long")
        self.assert_code(make_wav(0.1), "audio_too_short")
        validate_wav(make_wav(12.0, rate=8000))  # exactly at the limit is accepted


class TranscriberTests(TestCase):
    def make(self, **kwargs):
        client = mock.MagicMock()
        client.audio.transcriptions.create.return_value = SimpleNamespace(**kwargs)
        return Transcriber(api_key="k", model="gpt-4o-mini-transcribe", client=client), client

    def test_successful_transcription(self):
        transcriber, client = self.make(text="  Ce am de făcut mâine?  ")
        self.assertEqual(transcriber.transcribe(SPEECH), "Ce am de făcut mâine?")
        kwargs = client.audio.transcriptions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-4o-mini-transcribe")
        self.assertEqual(kwargs["file"], ("recording.wav", SPEECH, "audio/wav"))

    def test_empty_transcription(self):
        transcriber, _ = self.make(text="")
        self.assertEqual(transcriber.transcribe(SPEECH), "")

    def test_silence_skips_api(self):
        transcriber, client = self.make(text="hallucination")
        self.assertEqual(transcriber.transcribe(make_wav(1.0, amplitude=50)), "")
        client.audio.transcriptions.create.assert_not_called()

    def test_invalid_audio_never_sent(self):
        transcriber, client = self.make(text="x")
        with self.assertRaises(AudioValidationError):
            transcriber.transcribe(b"not audio")
        client.audio.transcriptions.create.assert_not_called()

    def test_api_failure(self):
        transcriber, client = self.make(text="x")
        req = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
        client.audio.transcriptions.create.side_effect = openai.APIConnectionError(request=req)
        with self.assertRaises(AssistantUnavailableError):
            transcriber.transcribe(SPEECH)

    def test_missing_key(self):
        with self.assertRaises(AssistantConfigError):
            Transcriber(api_key="").transcribe(SPEECH)


# --- speech ------------------------------------------------------------------------

class SpeechTests(TestCase):
    def make(self, pcm=b"\x10\x00" * 24000, **kwargs):
        client = mock.MagicMock()
        client.audio.speech.create.return_value = SimpleNamespace(read=lambda: pcm)
        return SpeechSynthesizer(api_key="k", client=client, **kwargs), client

    def test_successful_tts_returns_truthful_wav(self):
        synth, client = self.make()
        wav = synth.synthesize("Am salvat taskul.")
        with wave.open(io.BytesIO(wav)) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()),
                             (24000, 1, 2))
            self.assertEqual(w.getnframes(), 24000)
        kwargs = client.audio.speech.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-4o-mini-tts")
        self.assertEqual(kwargs["voice"], "marin")
        self.assertEqual(kwargs["response_format"], "pcm")
        self.assertIn("British English", kwargs["instructions"])

    def test_voice_configurable(self):
        synth, client = self.make(voice="cedar")
        synth.synthesize("x")
        self.assertEqual(client.audio.speech.create.call_args.kwargs["voice"], "cedar")

    def test_server_side_resampling(self):
        synth, _ = self.make(output_rate=16000)
        with wave.open(io.BytesIO(synth.synthesize("x"))) as w:
            self.assertEqual(w.getframerate(), 16000)
            self.assertEqual(w.getnframes(), 16000)  # 1 s at 24 kHz -> 1 s at 16 kHz
        self.assertEqual(len(resample_pcm16(b"\x00\x00" * 480, 24000, 8000)), 320)

    def test_invalid_tts_audio(self):
        synth, _ = self.make(pcm=b"\x00\x00\x00")
        with self.assertRaises(AssistantUnavailableError):
            synth.synthesize("x")

    def test_tts_api_failure(self):
        synth, client = self.make()
        req = httpx.Request("POST", "https://api.openai.com/v1/audio/speech")
        client.audio.speech.create.side_effect = openai.InternalServerError(
            "boom", response=httpx.Response(500, request=req), body=None)
        with self.assertRaises(AssistantUnavailableError):
            synth.synthesize("x")

    def test_spoken_text_is_bounded(self):
        long_text = "Prima propoziție este aici. " * 40
        self.assertLessEqual(len(spoken_text(long_text)), 600)
        self.assertTrue(spoken_text(long_text).endswith("."))


# --- voice service: pipeline and idempotency -----------------------------------------

class VoicePipelineTests(VoiceTestCase):
    def test_successful_voice_command(self):
        self.script_create()
        result = self.service.handle_voice(str(uuid.uuid4()), SPEECH)
        self.assertEqual(result.http_status, 200)
        payload = result.payload
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["transcript"], "Amintește-mi mâine la 10 să sun la dentist.")
        self.assertEqual(payload["reply"], "Am salvat taskul pentru mâine la ora 10.")
        self.assertTrue(payload["audio_ready"])
        self.assertEqual(payload["audio_url"], f"/api/voice/audio/{payload['request_id']}/")
        self.assertEqual(AgendaItem.objects.count(), 1)
        self.assertEqual(self.synth.calls, ["Am salvat taskul pentru mâine la ora 10."])

    def test_orchestrator_receives_transcript(self):
        self.script_create()
        self.service.handle_voice(str(uuid.uuid4()), SPEECH)
        first_input = self.fake.calls[0]["input"]
        self.assertEqual(first_input[-1], {"role": "user",
                                           "content": "Amintește-mi mâine la 10 să sun la dentist."})

    def test_raw_recording_is_not_stored(self):
        self.script_create()
        rid = uuid.uuid4()
        self.service.handle_voice(str(rid), SPEECH)
        rec = VoiceRequest.objects.get(pk=rid)
        self.assertEqual(rec.payload_sha256, payload_digest(SPEECH))
        self.assertNotEqual(bytes(rec.audio), SPEECH)  # only the generated reply audio

    def test_empty_transcription_is_no_speech(self):
        self.transcriber.results = [""]
        result = self.service.handle_voice(str(uuid.uuid4()), SPEECH)
        self.assertEqual(result.payload["status"], "no_speech")
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(result.payload["audio_ready"])

    def test_invalid_audio_rejected_without_record(self):
        result = self.service.handle_voice(str(uuid.uuid4()), b"junk")
        self.assertEqual(result.http_status, 415)
        self.assertEqual(VoiceRequest.objects.count(), 0)
        self.assertEqual(self.transcriber.calls, 0)

    def test_invalid_request_id(self):
        self.assertEqual(self.service.handle_voice("not-a-uuid", SPEECH).http_status, 400)

    def test_transcription_failure_is_retryable(self):
        self.transcriber.results = [AssistantUnavailableError("down"),
                                    "Amintește-mi mâine la 10 să sun la dentist."]
        rid = str(uuid.uuid4())
        first = self.service.handle_voice(rid, SPEECH)
        self.assertEqual(first.http_status, 502)
        self.assertTrue(first.payload["retryable"])
        self.script_create()
        second = self.service.handle_voice(rid, SPEECH)
        self.assertEqual(second.payload["status"], "success")
        self.assertEqual(AgendaItem.objects.count(), 1)

    def test_tts_failure_after_mutation_preserves_result_and_regenerates(self):
        self.synth.fail = True
        self.script_create()
        rid = str(uuid.uuid4())
        result = self.service.handle_voice(rid, SPEECH)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.payload["status"], "success")
        self.assertFalse(result.payload["audio_ready"])
        self.assertIsNotNone(result.payload["audio_url"])
        self.assertEqual(AgendaItem.objects.count(), 1)
        rec = VoiceRequest.objects.get(pk=rid)
        self.assertEqual((rec.status, rec.stage), ("COMPLETED", "EXECUTED"))

        # Audio regenerated later: agenda operation is NOT repeated.
        calls_before = len(self.fake.calls)
        self.synth.fail = False
        status, wav, _ = self.service.get_audio(rid)
        self.assertEqual(status, 200)
        self.assertEqual(wav, REPLY_WAV)
        self.assertEqual(len(self.fake.calls), calls_before)
        self.assertEqual(AgendaItem.objects.count(), 1)

    def test_orchestrator_failure_without_mutation_is_retryable(self):
        self.fake.add(AssistantUnavailableError("down"))
        rid = str(uuid.uuid4())
        first = self.service.handle_voice(rid, SPEECH)
        self.assertEqual(first.http_status, 502)
        self.assertTrue(first.payload["retryable"])
        self.script_create()
        self.assertEqual(self.service.handle_voice(rid, SPEECH).payload["status"], "success")
        self.assertEqual(AgendaItem.objects.count(), 1)

    def test_partial_failure_after_mutation_is_never_rerun(self):
        self.fake.add(response(create_task_call()), AssistantUnavailableError("down"))
        rid = str(uuid.uuid4())
        first = self.service.handle_voice(rid, SPEECH)
        self.assertEqual(first.payload["status"], "error")
        self.assertFalse(first.payload["retryable"])
        self.assertIn("salvate în agendă", first.payload["reply"])
        second = self.service.handle_voice(rid, SPEECH)
        self.assertEqual(second.payload, first.payload | {"audio_ready": second.payload["audio_ready"]})
        self.assertEqual(AgendaItem.objects.count(), 1)
        self.assertEqual(self.transcriber.calls, 1)


class IdempotencyTests(VoiceTestCase):
    def test_duplicate_request_id_returns_original_outcome(self):
        self.script_create()
        rid = str(uuid.uuid4())
        first = self.service.handle_voice(rid, SPEECH)
        second = self.service.handle_voice(rid, SPEECH)
        self.assertEqual(second.http_status, 200)
        self.assertEqual(second.payload, first.payload)
        self.assertEqual(AgendaItem.objects.count(), 1)
        self.assertEqual(self.transcriber.calls, 1)
        self.assertEqual(len(self.synth.calls), 1)

    def test_same_id_different_audio_rejected(self):
        self.script_create()
        rid = str(uuid.uuid4())
        self.service.handle_voice(rid, SPEECH)
        conflict = self.service.handle_voice(rid, OTHER_SPEECH)
        self.assertEqual(conflict.http_status, 409)
        self.assertEqual(conflict.payload["error"], "request_id_conflict")
        self.assertEqual(AgendaItem.objects.count(), 1)

    def test_request_in_progress_is_not_executed_twice(self):
        rid = uuid.uuid4()
        VoiceRequest.objects.create(id=rid, payload_sha256=payload_digest(SPEECH),
                                    stage=VoiceRequest.Stage.EXECUTING)
        result = self.service.handle_voice(str(rid), SPEECH)
        self.assertEqual(result.http_status, 202)
        self.assertEqual(result.payload["status"], "processing")
        self.assertEqual(self.transcriber.calls, 0)

    def stale(self, rid, stage):
        VoiceRequest.objects.create(id=rid, payload_sha256=payload_digest(SPEECH), stage=stage,
                                    transcript="Amintește-mi mâine la 10 să sun la dentist.")
        VoiceRequest.objects.filter(pk=rid).update(
            updated_at=timezone.now() - timedelta(seconds=STALE_PROCESSING_SECONDS + 5))

    def test_interrupted_during_execution_is_not_rerun(self):
        rid = uuid.uuid4()
        self.stale(rid, VoiceRequest.Stage.EXECUTING)
        result = self.service.handle_voice(str(rid), SPEECH)
        self.assertEqual(result.payload["status"], "error")
        self.assertEqual(result.payload["error"], "interrupted")
        self.assertFalse(result.payload["retryable"])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(AgendaItem.objects.count(), 0)
        # The status endpoint reports the same.
        self.assertEqual(self.service.get_status(str(rid)).payload["error"], "interrupted")

    def test_interrupted_before_execution_is_safely_resumed(self):
        rid = uuid.uuid4()
        self.stale(rid, VoiceRequest.Stage.TRANSCRIBED)
        self.script_create()
        result = self.service.handle_voice(str(rid), SPEECH)
        self.assertEqual(result.payload["status"], "success")
        self.assertEqual(AgendaItem.objects.count(), 1)

    def test_status_endpoint_for_interrupted_poll(self):
        rid = uuid.uuid4()
        VoiceRequest.objects.create(id=rid, payload_sha256="x", stage="EXECUTING")
        self.assertEqual(self.service.get_status(str(rid)).payload["status"], "processing")
        self.assertEqual(self.service.get_status(str(uuid.uuid4())).http_status, 404)

    def test_expired_audio_is_purged_and_regenerated_without_rerun(self):
        self.script_create()
        rid = str(uuid.uuid4())
        self.service.handle_voice(rid, SPEECH)
        VoiceRequest.objects.filter(pk=rid).update(
            audio_expires_at=timezone.now() - timedelta(seconds=1))
        status, wav, _ = self.service.get_audio(rid)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.synth.calls), 2)
        self.assertEqual(len(self.fake.calls), 2)  # still only the original two model calls
        self.assertEqual(AgendaItem.objects.count(), 1)

    def test_old_records_are_purged(self):
        old = VoiceRequest.objects.create(id=uuid.uuid4(), payload_sha256="x",
                                          status="COMPLETED")
        busy = VoiceRequest.objects.create(id=uuid.uuid4(), payload_sha256="y")
        VoiceRequest.objects.filter(pk__in=[old.pk, busy.pk]).update(
            created_at=timezone.now() - timedelta(days=10))
        self.service.get_audio(str(uuid.uuid4()))
        self.assertFalse(VoiceRequest.objects.filter(pk=old.pk).exists())
        self.assertTrue(VoiceRequest.objects.filter(pk=busy.pk).exists())

    def test_audio_for_unknown_or_processing_request(self):
        self.assertEqual(self.service.get_audio(str(uuid.uuid4()))[0], 404)
        rid = uuid.uuid4()
        VoiceRequest.objects.create(id=rid, payload_sha256="x")
        self.assertEqual(self.service.get_audio(str(rid))[0], 409)


class ConcurrentDuplicateTests(TransactionTestCase):
    """Two simultaneous uploads with the same request_id must execute once."""

    def test_concurrent_duplicate_requests_create_one_task(self):
        fake = FakeResponsesClient()
        fake.add(response(create_task_call()), response(message("Am salvat taskul.")))

        class SlowTranscriber(FakeTranscriber):
            def transcribe(self, audio):
                time.sleep(0.3)
                return super().transcribe(audio)

        transcriber = SlowTranscriber("Adaugă task.")
        service = VoiceService(transcriber=transcriber, synthesizer=FakeSynth(),
                               orchestrator=Orchestrator(client=fake, clock=fixed_clock()),
                               session_getter=ConversationSession, clock=fixed_clock())
        rid = str(uuid.uuid4())
        results, errors = [], []

        def call():
            from django.db import connection
            try:
                results.append(service.handle_voice(rid, SPEECH))
            except Exception as exc:  # surfaced below; a thread must not fail silently
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=call) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 3)
        self.assertEqual(AgendaItem.objects.count(), 1)
        self.assertEqual(transcriber.calls, 1)
        self.assertEqual(sorted(r.http_status for r in results)[0], 200)
        self.assertTrue(all(r.http_status in (200, 202, 429) for r in results))


class SpokenDeleteTests(VoiceTestCase):
    def test_spoken_no_never_confirms_deletion(self):
        item = agenda.create_item(type="TASK", title="Sună la bancă")
        args = {"item_id": str(item.id)}
        self.transcriber.results = ["Șterge taskul cu banca.", "Nu."]
        self.fake.add(response(function_call("delete_item", {**args, "confirmed": False})),
                      response(message("Sigur vrei să îl șterg?")))
        self.service.handle_voice(str(uuid.uuid4()), SPEECH)

        def check(instructions, input_items):
            self.assertEqual(tool_outputs(input_items)[-1]["error"], "not_confirmed")
            return response(message("Bine, nu îl șterg."))

        self.fake.add(response(function_call("delete_item", {**args, "confirmed": True})), check)
        result = self.service.handle_voice(str(uuid.uuid4()), OTHER_SPEECH)
        self.assertEqual(result.payload["transcript"], "Nu.")
        self.assertTrue(AgendaItem.objects.filter(pk=item.pk).exists())


class AgendaSummaryTests(VoiceTestCase):
    def test_summary_reads_real_records(self):
        agenda.create_item(type="APPOINTMENT", title="Dentist",
                           starts_at=FIXED_NOW + timedelta(hours=5))
        agenda.create_item(type="TASK", title="Citește cartea")
        text = build_agenda_summary(FIXED_NOW.date(), "ro")
        self.assertIn("marți 22 septembrie", text)
        self.assertIn("la 15:00, programare: Dentist", text)
        self.assertIn("Taskuri fără dată: Citește cartea", text)
        self.assertIn("appointment: Dentist", build_agenda_summary(FIXED_NOW.date(), "en"))

    def test_empty_day(self):
        self.assertIn("nu ai nimic", build_agenda_summary(FIXED_NOW.date(), "ro"))

    def test_summary_request_is_idempotent_and_read_only(self):
        rid = str(uuid.uuid4())
        first = self.service.handle_agenda_summary(rid, "ro")
        second = self.service.handle_agenda_summary(rid, "ro")
        self.assertEqual(first.http_status, 200)
        self.assertEqual(first.payload, second.payload)
        self.assertEqual(len(self.synth.calls), 1)
        self.assertEqual(self.fake.calls, [])  # no LLM, no agenda mutation
        self.assertEqual(self.service.handle_agenda_summary(rid, "en").http_status, 409)


# --- HTTP API ----------------------------------------------------------------------

@override_settings(DEVICE_API_TOKEN=TOKEN, SECURE_SSL_REDIRECT=False)
class VoiceApiTests(VoiceTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(views, "get_voice_service", return_value=self.service)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}

    def upload(self, audio=SPEECH, request_id=None, auth=True):
        data = {"request_id": request_id or str(uuid.uuid4())}
        if audio is not None:
            data["audio"] = io.BytesIO(audio)
            data["audio"].name = "rec.wav"
        return self.client.post(reverse("assistant:voice"), data=data,
                                **(self.auth if auth else {}))

    def test_voice_upload_and_protected_audio_download(self):
        self.script_create()
        res = self.upload()
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "success")
        self.assertNotIn("audio_base64", body)
        audio = self.client.get(body["audio_url"], **self.auth)
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio["Content-Type"], "audio/wav")
        self.assertEqual(audio["Cache-Control"], "no-store")
        self.assertEqual(audio.content, REPLY_WAV)
        self.assertEqual(self.client.get(body["audio_url"]).status_code, 401)
        status = self.client.get(reverse("assistant:voice-status", args=[body["request_id"]]),
                                 **self.auth)
        self.assertEqual(status.json()["reply"], body["reply"])

    def test_invalid_device_token(self):
        self.assertEqual(self.upload(auth=False).status_code, 401)
        res = self.client.post(reverse("assistant:voice"), {"request_id": str(uuid.uuid4())},
                               HTTP_AUTHORIZATION="Bearer wrong")
        self.assertEqual(res.status_code, 401)
        rid = str(uuid.uuid4())
        for name in ("assistant:voice-status", "assistant:voice-audio"):
            self.assertEqual(self.client.get(reverse(name, args=[rid])).status_code, 401)
        self.assertEqual(self.transcriber.calls, 0)

    def test_payload_errors(self):
        self.assertEqual(self.upload(audio=None).status_code, 400)
        self.assertEqual(self.upload(request_id="nope").status_code, 400)
        self.assertEqual(self.upload(audio=b"ID3junk").status_code, 415)
        self.assertEqual(self.upload(audio=make_wav(13.5, rate=8000)).status_code, 400)
        self.assertEqual(self.client.get(reverse("assistant:voice"), **self.auth).status_code, 405)

    def test_oversized_upload_rejected_before_parsing(self):
        res = self.client.post(reverse("assistant:voice"), data=b"x", content_type="audio/wav",
                               CONTENT_LENGTH=str(max_upload_bytes() + 100_000), **self.auth)
        self.assertEqual(res.status_code, 413)

    def test_duplicate_upload_over_http(self):
        self.script_create()
        rid = str(uuid.uuid4())
        self.assertEqual(self.upload(request_id=rid).status_code, 200)
        self.assertEqual(self.upload(request_id=rid).status_code, 200)
        self.assertEqual(self.upload(audio=OTHER_SPEECH, request_id=rid).status_code, 409)
        self.assertEqual(AgendaItem.objects.count(), 1)

    def test_agenda_summary_endpoint(self):
        res = self.client.post(reverse("assistant:voice-agenda"),
                               data=json.dumps({"request_id": str(uuid.uuid4())}),
                               content_type="application/json", **self.auth)
        self.assertEqual(res.status_code, 200)
        self.assertIn("nu ai nimic", res.json()["reply"])
        bad = self.client.post(reverse("assistant:voice-agenda"), data="{",
                               content_type="application/json", **self.auth)
        self.assertEqual(bad.status_code, 400)

    def test_existing_endpoints_still_work(self):
        self.assertEqual(self.client.get(reverse("assistant:health")).status_code, 200)
        self.assertEqual(self.client.get(reverse("assistant:agenda-today"), **self.auth)
                         .status_code, 200)
