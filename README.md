# Stick Assistant

A personal AI assistant for one person and one device, an M5Stack M5StickS3.
It understands Romanian and English, keeps a persistent agenda of **tasks** and
**appointments** in SQLite, and uses the OpenAI Responses API with function
calling to turn natural language into validated database operations.

```
You: Amintește-mi mâine la 10 să sun la dentist.
Assistant: Am salvat taskul: Sună la dentist, mâine la ora 10.
You: Mută-l la ora 12.
Assistant: Am mutat taskul la ora 12.
You: Marchează-l ca finalizat.
Assistant: Am marcat taskul ca finalizat.
```

- **Stage 1:** Django backend, agenda, OpenAI function calling, terminal chat
  and a JSON API.
- **Stage 2:** voice. The M5StickS3 records while you hold a button. The server
  transcribes the recording (`gpt-4o-mini-transcribe`), runs the same
  orchestrator, and answers with speech (`gpt-4o-mini-tts`, voice `marin`),
  which the device plays. Firmware and flashing instructions:
  [firmware/README.md](firmware/README.md).

---

## 1. Overview

- **Persistent memory:** agenda items live in SQLite (`backend/db.sqlite3`), so
  they survive restarts. Conversation context lives in memory only.
- **No fabricated results:** the model can only change data through six tools.
  The server validates every argument, and a tool reports `"ok": true` only
  after the database operation actually succeeded.
- **Safe deletes:** a delete runs only when the user's *next* message, after
  the assistant asked, is an explicit yes ("da", "yes", "confirm", "șterge-l").
  The server checks the user's own words, not only the model's flag. Any
  negation ("nu", "no, wait") cancels the request, and an unanswered request
  expires after one turn.
- **Timezone:** Europe/London, with correct GMT/BST handling. Non-existent local
  times (the spring-forward gap) are rejected.

## 2. Architecture

```
terminal (assistant_chat)  ─┐
                            ├─► Orchestrator ──► OpenAI Responses API (store=False)
HTTP API (/api/chat/)      ─┘        │                 │ function calls
                                     ▼                 ▼
                              ConversationSession   tools.py  (strict JSON schemas,
                              (bounded history,      │        server-side validation)
                               recent item UUIDs,    ▼
                               pending delete)     agenda.py (Django services, transactions)
                                                     │
                                                     ▼
                                              AgendaItem (SQLite)
```

| Path | Responsibility |
|---|---|
| `backend/assistant/models.py` | `AgendaItem` model: validation, status transitions, querysets |
| `backend/assistant/services/agenda.py` | CRUD business logic (`create_item`, `list_items`, `get_item`, `update_item`, `complete_item`, `delete_item`) |
| `backend/assistant/services/datetime_utils.py` | Parsing local dates/times, DST handling, relative days, calendar context for the model |
| `backend/assistant/services/tools.py` | Tool JSON schemas, argument validation, dispatch, delete confirmation |
| `backend/assistant/services/openai_client.py` | OpenAI SDK wrapper, error mapping |
| `backend/assistant/services/orchestrator.py` | Tool-calling loop, prompt, conversation handling |
| `backend/assistant/services/session.py` | Bounded in-memory session state |
| `backend/assistant/management/commands/assistant_chat.py` | Terminal interface |
| `backend/assistant/views.py` | JSON API (health, chat, agenda, voice endpoints) |
| `backend/assistant/services/transcription.py` | WAV validation, silence gate, OpenAI speech-to-text |
| `backend/assistant/services/speech.py` | OpenAI text-to-speech → PCM WAV (optional server-side resampling) |
| `backend/assistant/services/voice.py` | Voice pipeline, idempotent `request_id` handling, Button B agenda summary |
| `firmware/` | M5StickS3 PlatformIO firmware (see its README) |

### Voice pipeline (Stage 2)

```
M5StickS3 ──WAV 16 kHz──► POST /api/voice/ ──► VoiceRequest row (request_id = PK, SHA-256)
                                               │
                        validate WAV ─► transcribe ─► Orchestrator (same as terminal/chat)
                                               │         └─ tools ─► agenda ─► SQLite
                              outcome committed to SQLite BEFORE speech is generated
                                               │
                        TTS (pcm 24 kHz) ─► WAV stored ≤ 1 h ─► GET /api/voice/audio/<id>/
```

**Idempotency.**
- The device's `request_id` is the primary key of `VoiceRequest`, so SQLite
  guarantees one execution per id. The recording's SHA-256 is stored with it;
  reusing an id with different audio returns `409`.
- Each request records its progress in `stage`: `RECEIVED` → `TRANSCRIBED` →
  `EXECUTING` → `EXECUTED`.
- A retry of a completed request returns the stored result without running
  anything again.
- A request that failed before `EXECUTING` (transcription or OpenAI failed, so
  nothing touched the agenda) may be re-run with the same id.
- A request interrupted during `EXECUTING` is never re-run; it is reported as
  `interrupted`.
- If speech generation fails, the database result is kept. Audio is regenerated
  on demand from the stored reply text, without repeating the agenda operation.

**Storage.** Raw recordings are only held in memory and never written to disk
or the database. Generated reply audio expires after `VOICE_AUDIO_TTL_SECONDS`.
Request records (hash, transcript, reply) are deleted after
`VOICE_REQUEST_RETENTION_HOURS`.

**How a message is handled:** the user message, the current date/time and
two-week calendar, and the recently referenced items (real UUIDs from SQLite)
are sent to OpenAI. The model returns function calls. Each call is validated
and run against the agenda service, and the real result goes back to the model.
This repeats for up to 6 rounds, then the model writes the final reply.
Because requests use `store=False`, the app resends the bounded history itself.
For reasoning models it also resends the encrypted reasoning items
(`include=["reasoning.encrypted_content"]`).

## 3. Installation (Windows PowerShell)

Requires Python 3.11.

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks the activation script, run this once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## 4. Environment configuration

Settings come from environment variables. For local development, put them in
`backend/.env`. That file is gitignored and never committed.

```powershell
# from the backend folder
Copy-Item ..\.env.example .env
notepad .env
```

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | yes | Your OpenAI API key. Keep it only in `.env` or your environment. |
| `OPENAI_MODEL` | no (default `gpt-5.6-luna`) | Any model that supports the Responses API and function calling. If the model is wrong, you get a clear error; the app never switches to another model on its own. |
| `OPENAI_REASONING_EFFORT` | no (default `low`) | `none`/`low`/`medium`/`high` for reasoning models; `off` omits the parameter |

Default model check (22/09/2026):
- `gpt-5.6-luna` is listed in OpenAI's model docs as the cost-optimised
  GPT-5.6 model. It supports the Responses API, function calling and
  structured outputs, with reasoning effort from `none` to `max`, at
  $0.20 / $1.20 per million input/output tokens.
- It is returned by `GET /v1/models` for the configured key.
- It passed the live check below using exactly the app's request parameters:
  `store=False`, `reasoning.effort=low`,
  `include=["reasoning.encrypted_content"]`, strict function tools and
  `parallel_tool_calls`.
| `OPENAI_TRANSCRIBE_MODEL` | no (default `gpt-4o-mini-transcribe`) | Speech-to-text model |
| `OPENAI_TTS_MODEL` | no (default `gpt-4o-mini-tts`) | Text-to-speech model |
| `OPENAI_TTS_VOICE` | no (default `marin`) | TTS voice, e.g. `marin` or `cedar` |
| `VOICE_MAX_SECONDS` | no (default `12`) | Longest accepted recording |
| `VOICE_AUDIO_TTL_SECONDS` | no (default `3600`) | How long generated reply audio is kept |
| `VOICE_REQUEST_RETENTION_HOURS` | no (default `72`) | How long request records (no audio) are kept for idempotency |
| `VOICE_OUTPUT_SAMPLE_RATE` | no (default `24000`) | Sample rate of reply WAVs (OpenAI PCM is 24 kHz; other rates are resampled on the server) |
| `DJANGO_SECRET_KEY` | yes in production | Long random string |
| `DJANGO_DEBUG` | no (default `False`) | `True` for local development |
| `DJANGO_ALLOWED_HOSTS` | no | Comma-separated, default `localhost,127.0.0.1` |
| `DEVICE_API_TOKEN` | yes for the API | Static token the device sends |

Generate random values for the secret key and device token:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

To set the key only for the current PowerShell session, without a file:

```powershell
$env:OPENAI_API_KEY = "sk-..."
```

Real environment variables override values in `.env`.

## 5. Database migrations

```powershell
python manage.py migrate
python manage.py createsuperuser   # optional, for the admin at http://127.0.0.1:8000/admin/
```

## 6. Terminal assistant

```powershell
python manage.py assistant_chat
```

Type messages in Romanian or English. Type `exit` (or press Ctrl+C) to quit.
Example commands:

- `Amintește-mi mâine la 10 să sun la dentist.`
- `Am programare la dentist vineri.` → the assistant asks: *La ce oră este programarea?*
- `Ce am de făcut mâine?` / `What do I have next week?`
- `Mută-l la ora 12.` / `Mută-l pe vineri.`
- `Marchează-l ca finalizat.`
- `Șterge taskul cu banca.` → the assistant asks for confirmation → `Da.`
- `Ce taskuri am finalizat?` (completed items are shown only when asked for)

## 7. Django server

```powershell
python manage.py runserver
```

## 8. HTTP API

Protected endpoints need `Authorization: Bearer <DEVICE_API_TOKEN>`.
`X-Device-Token: <token>` also works. Use HTTPS in production.

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/health/` | no | `{"status": "ok", "database": "ok"}` |
| POST | `/api/chat/` | yes | `{"message": "..."}` → `{"reply": "...", "status": "success"}` |
| GET | `/api/agenda/today/` | yes | Today's active items plus undated pending tasks |

The chat endpoint uses the same orchestrator as the terminal. It keeps a single
device conversation in memory, which resets after 30 minutes of inactivity.

PowerShell:

```powershell
$token = "your-device-token"
$headers = @{ Authorization = "Bearer $token" }

Invoke-RestMethod http://127.0.0.1:8000/api/health/

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/chat/ `
  -Headers $headers -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes('{"message": "Ce am de făcut mâine?"}'))

Invoke-RestMethod -Uri http://127.0.0.1:8000/api/agenda/today/ -Headers $headers
```

curl:

```bash
curl -X POST http://127.0.0.1:8000/api/chat/ \
  -H "Authorization: Bearer your-device-token" \
  -H "Content-Type: application/json" \
  -d '{"message": "What do I have tomorrow?"}'
```

### Voice endpoints (Stage 2)

All voice endpoints require the device token.

| Method | Path | Body / response |
|---|---|---|
| POST | `/api/voice/` | `multipart/form-data`: `audio` (WAV, mono, 16-bit PCM, 8–48 kHz, 0.3–12 s) and `request_id` (UUID made by the device; reuse it on retries). Response JSON below. |
| GET | `/api/voice/requests/<request_id>/` | The same JSON; use it after an interrupted upload or while `status` is `processing` |
| GET | `/api/voice/audio/<request_id>/` | `audio/wav` (binary, mono 16-bit PCM, 24 kHz by default). Regenerated from the stored reply if expired; the agenda is never touched. |
| POST | `/api/voice/agenda/` | JSON `{"request_id": "<uuid>", "language": "ro"}` (or `"en"`). Returns a spoken summary of today's agenda, built from SQLite (read-only, no AI call). |

Voice response JSON (never contains base64 audio):

```json
{
  "request_id": "0b3c…",
  "status": "success",
  "stage": "EXECUTED",
  "transcript": "Amintește-mi mâine la 10 să sun la dentist.",
  "reply": "Am salvat taskul pentru mâine la ora 10.",
  "error": null,
  "retryable": false,
  "audio_url": "/api/voice/audio/0b3c…/",
  "audio_ready": true
}
```

- `status` values:
  - `success`;
  - `no_speech`: silence or an empty transcript; `reply` asks to try again;
  - `processing`: HTTP 202;
  - `error`: `reply` holds a message that can be spoken;
  - `busy`: HTTP 429.
- Voice HTTP codes:
  - `400`: bad `request_id`, corrupt, too short or too long audio;
  - `409`: `request_id` reused with different audio;
  - `411`: no Content-Length;
  - `413`: too large;
  - `415`: not 16-bit mono PCM WAV;
  - `502`: failure. Check `retryable`: `true` means nothing touched the agenda,
    so the same id can be retried.

Test from the PC with a fixture recording:

```powershell
$id = [guid]::NewGuid().ToString()
curl.exe -s -H "Authorization: Bearer $token" `
  -F "request_id=$id" -F "audio=@backend/assistant/fixtures/voice/ro_query_tomorrow.wav;type=audio/wav" `
  http://127.0.0.1:8000/api/voice/
curl.exe -s -H "Authorization: Bearer $token" -o reply.wav http://127.0.0.1:8000/api/voice/audio/$id/
```

Status codes (all endpoints):

| Code | Meaning |
|---|---|
| `400` | Invalid payload (bad JSON, missing, empty or >1000-character `message`) |
| `401` | Missing or invalid token |
| `405` | Wrong method |
| `413` | Body larger than 64 KB |
| `429` | Another chat request is still running. Retry shortly; this stops a retry from repeating an operation. |
| `500` | Unexpected server or database error (generic message only) |
| `502` | AI service or configuration error. If the database changed before the failure, the reply says so. |
| `503` | `DEVICE_API_TOKEN` not configured on the server |

Error responses never include stack traces, internal details or the OpenAI key.
Only `/api/chat/` waits on the device session; `/api/health/` and
`/api/agenda/today/` are never blocked by a running OpenAI request.

With `DJANGO_DEBUG=False`, plain HTTP requests are redirected to HTTPS
(`DJANGO_SECURE_SSL_REDIRECT=False` turns this off if TLS is terminated
elsewhere).

## 9. Tests

```powershell
python manage.py test assistant -v 2
python manage.py check
python manage.py check --deploy
python manage.py makemigrations --check --dry-run
```

The tests never call OpenAI. The model is replaced by a scripted fake client
(`assistant/tests/helpers.py`) and the clock is fixed, so runs are
deterministic. They give the same result with `DJANGO_DEBUG` set to `True` or
`False`. The tests cover models, the agenda service, date and DST handling,
tool validation, the orchestration loop, follow-up references, ambiguity,
delete confirmation, error handling and the HTTP API. Regression tests for
defects found in the audit are in `test_regressions.py`.

**CI:** `.github/workflows/backend-tests.yml` runs on every push to `main` and
on every pull request to `main`, using Python 3.11, safe dummy settings and no
OpenAI key, so it makes no paid API calls. It has two jobs:
- the backend checks and the full test suite;
- a PlatformIO build of the M5StickS3 firmware with placeholder secrets and no
  hardware.

The voice tests (`test_voice.py`) use generated WAVs and fake OpenAI clients.
They cover:
- WAV validation (empty, corrupted, wrong format, too large, too long);
- transcription and TTS success and failure;
- a TTS failure after a successful agenda change;
- protected and expired audio;
- duplicate or conflicting `request_id`s, and concurrent duplicates (real
  threads on a file-based SQLite test database);
- recovery after an interruption;
- audio regenerated without repeating the agenda operation;
- a spoken "Nu" that must not confirm a deletion.

**Live voice check (optional, real OpenAI audio + LLM calls, costs a few cents):**

```powershell
python manage.py assistant_voice_check
```

This command:
- uploads the real speech recordings in `backend/assistant/fixtures/voice/`
  (16 kHz mono WAV) through the actual HTTP voice endpoints;
- checks SQLite after each step (create, retry without duplicate, 409, query,
  "Mută-l la ora 12", Button B summary, delete then "Nu", English);
- validates the returned WAV audio;
- removes only the records it created.

It never runs in CI.

**Live OpenAI check (optional, uses your API key and costs a few API calls):**

```powershell
python manage.py assistant_live_check
```

This runs a real Romanian and English conversation: create a task, list it,
move it with "Mută-l la ora 12", complete it, list completed tasks, ask for a
missing appointment time, create an appointment, list it, and delete it with
confirmation. It checks SQLite after each step, then removes only the records
it created. **A passing unit test suite does not prove the live connection
works; this command does.**

## 10. Current limitations

- Conversation context is in memory only and is lost on restart. Agenda data is
  not lost.
- There is a single device session for the HTTP API. The single-process dev
  server is assumed; multiple workers would each keep their own context.
- No reminders or notifications, and no recurring items.
- Relative dates are resolved by the model from the calendar supplied in the
  prompt. The server checks the resulting format and validity, but cannot know
  whether the model picked the day the user meant. Replies say the date back so
  the user can correct it.
- "next Friday" means the Friday of next week (Monday–Sunday).
- In the ambiguous autumn DST hour, the first (BST) occurrence is used, and the
  assistant is told to mention this to the user.
- Checking a delete confirmation relies on a list of yes/no words in Romanian
  and English. Unusual phrasing is treated as "not confirmed", which is the safe
  outcome: the assistant simply asks again.
- `check --deploy` warns about HSTS until `DJANGO_HSTS_SECONDS` is set. Enable it
  only when the site is served exclusively over HTTPS.

- **Voice:**
  - Push-to-talk only; there is no wake word or continuous listening.
  - The firmware compiles in CI but still needs to be checked on a physical
    M5StickS3 (see the checklist in `firmware/README.md`).
  - Reply audio is downloaded completely before playback starts.
  - The display shows Romanian text without diacritics.
  - Transcription can mishear, so the assistant asks you to repeat when a
    command is unclear.
- Voice and `/api/chat/` share one device conversation per server process. The
  terminal chat has its own session.

## 11. Next stage

- Stage 3 (not started): for example reminders or notifications, streaming
  audio playback, and HTTPS deployment for use outside the home network.
