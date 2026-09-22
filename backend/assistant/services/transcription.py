"""
Speech-to-text for device recordings.

Recordings are validated strictly (RIFF/WAVE, PCM, mono, 16-bit, known sample
rate, bounded size and duration) before anything is sent to OpenAI. Audio is
handled in memory only and never written to disk or the database.
"""

from __future__ import annotations

import io
import logging
import struct
import wave
from array import array
from dataclasses import dataclass

import openai
from django.conf import settings

from .openai_client import build_openai_client, map_openai_error

logger = logging.getLogger("assistant.transcription")

SUPPORTED_SAMPLE_RATES = {8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000}
# Peak amplitude below which a recording is treated as silence (int16 full scale 32767).
SILENCE_PEAK_THRESHOLD = 300
TRANSCRIPTION_PROMPT = (
    "A person speaking Romanian or English to a personal agenda assistant: tasks, "
    "appointments, dates and times (e.g. 'mâine la ora 10', 'next Friday at 14:30')."
)


class AudioValidationError(ValueError):
    """The uploaded recording is not acceptable. ``code`` is safe to return to the device."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    channels: int
    sample_width: int
    frames: int

    @property
    def duration(self) -> float:
        return self.frames / self.sample_rate


def max_upload_bytes() -> int:
    """Largest acceptable WAV: max duration at the highest supported rate, plus headers."""
    return int(settings.VOICE_MAX_SECONDS * max(SUPPORTED_SAMPLE_RATES) * 2) + 4096


def validate_wav(data: bytes) -> WavInfo:
    if not data:
        raise AudioValidationError("empty_audio", "The recording is empty.")
    if len(data) > max_upload_bytes():
        raise AudioValidationError("audio_too_large", "The recording is too large.")
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioValidationError("unsupported_format", "The recording must be a WAV file.")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            info = WavInfo(wav.getframerate(), wav.getnchannels(), wav.getsampwidth(),
                           wav.getnframes())
            pcm = wav.readframes(info.frames)
    except wave.Error as exc:
        if "unknown format" in str(exc):  # e.g. float, A-law, extensible
            raise AudioValidationError("unsupported_format",
                                       "The recording must be PCM WAV.") from exc
        raise AudioValidationError("corrupted_audio", f"The WAV file is invalid: {exc}") from exc
    except (EOFError, struct.error) as exc:
        raise AudioValidationError("corrupted_audio", f"The WAV file is invalid: {exc}") from exc

    if info.channels != 1:
        raise AudioValidationError("unsupported_format", "The recording must be mono.")
    if info.sample_width != 2:
        raise AudioValidationError("unsupported_format", "The recording must be 16-bit PCM.")
    if info.sample_rate not in SUPPORTED_SAMPLE_RATES:
        raise AudioValidationError("unsupported_format",
                                   f"Unsupported sample rate {info.sample_rate} Hz.")
    if len(pcm) != info.frames * 2:
        raise AudioValidationError("corrupted_audio", "The WAV data chunk is truncated.")
    if info.frames == 0:
        raise AudioValidationError("empty_audio", "The recording contains no audio.")
    if info.duration < settings.VOICE_MIN_SECONDS:
        raise AudioValidationError("audio_too_short", "The recording is too short.")
    if info.duration > settings.VOICE_MAX_SECONDS + 0.5:
        raise AudioValidationError(
            "audio_too_long", f"The recording is longer than {settings.VOICE_MAX_SECONDS:g} s.")
    return info


def pcm_peak(data: bytes) -> int:
    """Peak absolute amplitude of the WAV's 16-bit PCM samples."""
    with wave.open(io.BytesIO(data), "rb") as wav:
        samples = array("h")
        samples.frombytes(wav.readframes(wav.getnframes()))
    if samples.itemsize != 2:  # pragma: no cover - platform guard
        raise RuntimeError("Unexpected array item size")
    return max((abs(s) for s in samples), default=0)


def is_silent(data: bytes) -> bool:
    return pcm_peak(data) < SILENCE_PEAK_THRESHOLD


class Transcriber:
    def __init__(self, api_key: str | None = None, model: str | None = None, client=None):
        self.api_key = api_key if api_key is not None else settings.OPENAI_API_KEY
        self.model = model or settings.OPENAI_TRANSCRIBE_MODEL
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = build_openai_client(self.api_key, settings.OPENAI_TIMEOUT_SECONDS)
        return self._client

    def transcribe(self, wav_bytes: bytes) -> str:
        """Validated WAV -> text. Returns "" for silence without calling OpenAI."""
        validate_wav(wav_bytes)
        if is_silent(wav_bytes):
            logger.info("Recording is silent; skipping transcription")
            return ""
        client = self.client
        try:
            result = client.audio.transcriptions.create(
                model=self.model,
                file=("recording.wav", wav_bytes, "audio/wav"),
                prompt=TRANSCRIPTION_PROMPT,
                response_format="json",
            )
        except openai.OpenAIError as exc:
            raise map_openai_error(exc, self.model) from exc
        text = (getattr(result, "text", "") or "").strip()
        logger.info("Transcribed %d characters", len(text))
        return text
