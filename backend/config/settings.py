"""
Django settings for the stick-assistant backend.

All secrets and environment-specific values come from environment variables.
For local development they may be placed in ``backend/.env`` (never committed).
"""

import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Real environment variables always take precedence over the .env file.
load_dotenv(BASE_DIR / ".env", override=False)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DEBUG = env_bool("DJANGO_DEBUG", default=False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        # Development-only fallback so the project runs out of the box.
        SECRET_KEY = "django-insecure-dev-only-key-change-me"
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

# --- Assistant configuration -------------------------------------------------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "") or "gpt-5.6-luna"
OPENAI_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "") or "low"
OPENAI_TIMEOUT_SECONDS = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "") or 30)
DEVICE_API_TOKEN = os.environ.get("DEVICE_API_TOKEN", "")

# --- Voice (Stage 2) ------------------------------------------------------------
OPENAI_TRANSCRIBE_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "") or "gpt-4o-mini-transcribe"
OPENAI_TTS_MODEL = os.environ.get("OPENAI_TTS_MODEL", "") or "gpt-4o-mini-tts"
OPENAI_TTS_VOICE = os.environ.get("OPENAI_TTS_VOICE", "") or "marin"
# Upload limits for device recordings (WAV, mono, 16-bit PCM).
VOICE_MAX_SECONDS = float(os.environ.get("VOICE_MAX_SECONDS", "") or 12)
VOICE_MIN_SECONDS = 0.3
# Generated reply audio is kept for this long, then regenerated on demand.
VOICE_AUDIO_TTL_SECONDS = int(os.environ.get("VOICE_AUDIO_TTL_SECONDS", "") or 3600)
# Request records (transcript + reply, never raw audio) are kept for idempotency.
VOICE_REQUEST_RETENTION_HOURS = int(os.environ.get("VOICE_REQUEST_RETENTION_HOURS", "") or 72)
# Output sample rate of the WAV sent to the device (OpenAI PCM is 24 kHz).
VOICE_OUTPUT_SAMPLE_RATE = int(os.environ.get("VOICE_OUTPUT_SAMPLE_RATE", "") or 24000)

# --- Application definition --------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "assistant",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            # Wait for a competing writer (e.g. a device retry) instead of failing
            # immediately, and take the write lock at BEGIN to avoid lock-upgrade
            # deadlocks between concurrent requests.
            "timeout": 20,
            "transaction_mode": "IMMEDIATE",
        },
        # File-based test database so tests see the same locking behaviour as
        # production (the default in-memory shared cache fails fast on contention).
        "TEST": {"NAME": BASE_DIR / "test_db.sqlite3"},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Internationalisation / time ---------------------------------------------
LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/London"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# The device only sends short text messages; reject large bodies early.
# (Voice uploads are size-checked separately in the voice views.)
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024
# Keep uploaded recordings in memory so raw audio is never written to disk.
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024

# --- Security (production) ---------------------------------------------------
if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "") or 0)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# --- Logging -----------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "loggers": {
        "assistant": {
            "handlers": ["console"],
            # Tests deliberately trigger error paths; keep their output readable.
            "level": "CRITICAL" if "test" in sys.argv[1:2] else
                     os.environ.get("ASSISTANT_LOG_LEVEL", "WARNING"),
            "propagate": False,
        },
        # The OpenAI / httpx loggers can print request details; keep them quiet.
        "openai": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "httpx": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
