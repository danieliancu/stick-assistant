"""
Text-to-speech for replies played on the device.

OpenAI's ``pcm`` output format is raw 16-bit signed little-endian mono at
24 kHz. The server wraps it in a standard WAV header (optionally resampled to
``VOICE_OUTPUT_SAMPLE_RATE``) so the device always receives plain PCM WAV with a
truthful header and never has to decode MP3.
"""

from __future__ import annotations

import io
import logging
import re
import wave
from array import array

import openai
from django.conf import settings

from .openai_client import AssistantUnavailableError, build_openai_client, map_openai_error

logger = logging.getLogger("assistant.speech")

OPENAI_PCM_SAMPLE_RATE = 24000
MAX_SPOKEN_CHARS = 600
MAX_AUDIO_SECONDS = 60

TTS_INSTRUCTIONS = (
    "You are a calm, friendly personal assistant speaking from a small device. "
    "Speak at a natural, slightly brisk pace. When the text is Romanian, speak natural "
    "native Romanian with correct diacritics pronunciation. When the text is English, use "
    "a British English accent. Read times and dates naturally."
)


def spoken_text(text: str, limit: int = MAX_SPOKEN_CHARS) -> str:
    """Trim text for speech: collapse whitespace and cut at a sentence boundary."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return (cut[: boundary + 1] if boundary > limit // 2 else cut.rstrip() + "…").strip()


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resampling of mono 16-bit PCM (adequate for speech)."""
    if src_rate == dst_rate or not pcm:
        return pcm
    src = array("h")
    src.frombytes(pcm)
    n_out = max(1, int(len(src) * dst_rate / src_rate))
    step = src_rate / dst_rate
    out = array("h", bytes(2 * n_out))
    last = len(src) - 1
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        if j >= last:
            out[i] = src[last]
        else:
            frac = pos - j
            out[i] = int(src[j] + (src[j + 1] - src[j]) * frac)
    return out.tobytes()


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


class SpeechSynthesizer:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 voice: str | None = None, output_rate: int | None = None, client=None):
        self.api_key = api_key if api_key is not None else settings.OPENAI_API_KEY
        self.model = model or settings.OPENAI_TTS_MODEL
        self.voice = voice or settings.OPENAI_TTS_VOICE
        self.output_rate = output_rate or settings.VOICE_OUTPUT_SAMPLE_RATE
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = build_openai_client(self.api_key, settings.OPENAI_TIMEOUT_SECONDS)
        return self._client

    def synthesize(self, text: str) -> bytes:
        """Text -> mono 16-bit PCM WAV bytes at ``output_rate``."""
        text = spoken_text(text)
        if not text:
            raise ValueError("Nothing to speak.")
        client = self.client
        try:
            response = client.audio.speech.create(
                model=self.model,
                voice=self.voice,
                input=text,
                instructions=TTS_INSTRUCTIONS,
                response_format="pcm",
            )
            pcm = response.read() if hasattr(response, "read") else bytes(response.content)
        except openai.OpenAIError as exc:
            raise map_openai_error(exc, self.model) from exc

        if not pcm or len(pcm) % 2:
            logger.error("TTS returned invalid PCM (%d bytes)", len(pcm or b""))
            raise AssistantUnavailableError("The speech service returned invalid audio.")
        max_bytes = MAX_AUDIO_SECONDS * OPENAI_PCM_SAMPLE_RATE * 2
        if len(pcm) > max_bytes:
            logger.warning("TTS audio longer than %s s; truncating", MAX_AUDIO_SECONDS)
            pcm = pcm[:max_bytes]
        pcm = resample_pcm16(pcm, OPENAI_PCM_SAMPLE_RATE, self.output_rate)
        return pcm_to_wav(pcm, self.output_rate)
