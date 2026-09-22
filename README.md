# Stick Assistant — Stage 1 backend

A personal AI assistant for one person and one device (a future M5StickS3).
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

Stage 1 is text-only: a terminal chat plus a small JSON API. No firmware, audio,
speech-to-text or text-to-speech yet.

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
| `backend/assistant/views.py` | JSON API (`/api/health/`, `/api/chat/`, `/api/agenda/today/`) |

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

Status codes:

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

**CI:** `.github/workflows/backend-tests.yml` runs the checks and the full suite
on every push to `main` and on every pull request to `main`. It uses Python 3.11,
safe dummy settings and no OpenAI key, so it makes no paid API calls.

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

## 11. Next stage (Stage 2)

- M5StickS3 firmware talking to `/api/chat/` over HTTPS.
- Audio endpoints: speech-to-text for input, text-to-speech for replies.
- Possibly persistent conversation state and reminders.
