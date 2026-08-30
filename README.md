# Sam

Sam is a private Telegram climbing companion for 2–4 fixed users. This repository implements a
usable first version: text and voice intake, durable raw history, structured workouts and recovery,
natural-language corrections, semantic memory, an initial plan, personalized coaching, and a local
product backlog.

The product source of truth is [SPEC.md](SPEC.md).

## What is included

- one allowlisted Telegram group;
- Telegram user ID → athlete mapping for Alexey and Andrey;
- text and voice-note processing;
- OpenAI structured extraction and separate coaching responses;
- workouts, daily state, weight, facts, corrections and provenance;
- raw messages, episodic/coach memories and pgvector retrieval;
- Alexey's seeded profile, target trip and initial plan hypothesis;
- an empty non-invented Andrey profile;
- explicit natural-language backlog creation;
- idempotent handling of repeated Telegram updates;
- a processing audit trail without hidden chain-of-thought.

Garmin screenshot extraction, advanced weekly analytics and GitHub Issue synchronization are not
included yet.

## Requirements

- Docker Desktop with Docker Compose, or Python 3.12+ and PostgreSQL with pgvector;
- a Telegram bot token from [@BotFather](https://t.me/BotFather);
- an OpenAI API key.

## Telegram setup

1. Open `@BotFather`, run `/newbot`, and save the token.
2. Add the bot to the private group.
3. In `@BotFather`, use `/setprivacy` and disable privacy for the bot. Sam needs to see ordinary
   group messages so he can learn silently, although he will not answer every message.
4. Obtain the numeric group chat ID and the numeric Telegram user IDs for Alexey and Andrey. A
   temporary bot such as `@RawDataBot`, or the debug update payload during initial setup, can show
   these values. Supergroup IDs normally start with `-100`.
5. Never identify athletes by display name; configure the immutable numeric IDs below.

## Configuration

Copy `.env.example` to `.env` and fill at least:

```env
TELEGRAM_BOT_TOKEN=123456:replace_me
TELEGRAM_ALLOWED_CHAT_ID=-1001234567890
ALEXEY_TELEGRAM_USER_ID=111111111
ANDREY_TELEGRAM_USER_ID=222222222
OPENAI_API_KEY=replace_me
DATABASE_URL=postgresql+asyncpg://sam:sam@postgres:5432/sam
```

Models are configurable. The defaults are examples, not hard-coded application requirements:

```env
SAM_MODEL=gpt-5.4-mini
EXTRACTION_MODEL=gpt-5.4-mini
TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
```

The current database vector columns have 1536 dimensions. Keep `EMBEDDING_DIMENSIONS=1536` unless a
migration changes the column type.

## Run with Docker

```powershell
Copy-Item .env.example .env
# edit .env
docker compose up --build
```

The app runs Alembic migrations before startup. Check health at
[`http://localhost:8000/health`](http://localhost:8000/health).

Stop without deleting data:

```powershell
docker compose down
```

## Run locally

Start a PostgreSQL 17 instance with pgvector, then:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
$env:DATABASE_URL = "postgresql+asyncpg://sam:sam@localhost:5432/sam"
alembic upgrade head
uvicorn app.main:app --reload
```

If Telegram or OpenAI credentials are absent, FastAPI still starts and reports the missing
configuration through `/health`, but polling is disabled.

## Deploy to Fly.io

The repository includes `fly.toml` for a single always-running machine in Toronto (`yyz`). The
machine must not auto-stop because Telegram long polling runs continuously.

Create the Fly app once:

```powershell
fly apps create sam-climbing --org personal
```

Provide a PostgreSQL URL with pgvector enabled, then set secrets without committing them:

```powershell
fly secrets set `
  TELEGRAM_BOT_TOKEN="..." `
  TELEGRAM_ALLOWED_CHAT_ID="-100..." `
  ALEXEY_TELEGRAM_USER_ID="..." `
  ANDREY_TELEGRAM_USER_ID="..." `
  OPENAI_API_KEY="..." `
  DATABASE_URL="postgresql+asyncpg://..."
```

Deploy and verify:

```powershell
fly deploy
fly status
fly logs
Invoke-RestMethod https://sam-climbing.fly.dev/health
```

Do not deploy with the default SQLite URL: a Fly Machine filesystem is ephemeral and Sam's memory
would be lost when the Machine is replaced.

### Low-cost unmanaged PostgreSQL on Fly

`fly.postgres.toml` defines a private single-node PostgreSQL 17 app using the official pgvector
image. It has no public service and is reachable only through Fly's private network. This is suitable
for the small initial group, but it is not managed or highly available. Fly volume snapshots are the
minimum recovery mechanism; periodically test restores and export an off-platform backup.

## How message processing works

```text
allowlisted Telegram update
  → immutable sender lookup
  → raw message persistence
  → structured extraction
  → validated DB mutations and correction audit
  → embedding + relevant-history retrieval
  → compact athlete context
  → Sam coaching response when a response is warranted
```

OpenAI response state is not Sam's database. Every durable fact and every assistant response is
stored locally in PostgreSQL. A failed embedding does not block saving a workout or replying.

## Acceptance checks in Telegram

### Conversation and persona

Send:

```text
Сэм, привет. Что ты обо мне знаешь?
```

For Alexey, the reply should mention only the seeded Alexey profile and treat the inside-corner
observation as preliminary. For Andrey, Sam must not invent history.

### Alexey/Andrey recognition

Ask the same question from both configured accounts. Then inspect:

An unmapped group member can send `/whoami`; Sam replies with that member's numeric Telegram user
ID. Add it as `ANDREY_TELEGRAM_USER_ID` and redeploy/update the Fly secret.

```powershell
docker compose exec postgres psql -U sam -d sam -c `
  "select name, telegram_user_id from athletes order by name;"
```

### Structured training memory

From Alexey:

```text
Сэм, сегодня сделал три 5.9 на автостраховке. Предплечья 6 из 10, боли нет.
```

Later ask:

```text
Сэм, что было на последней тренировке и что сегодня лучше сделать?
```

### Voice note

Send the same training description as a Telegram voice note. The `messages` row should contain the
raw transcript and transcription metadata; the corresponding workout should cite that message.

### Correction

After recording a workout, send:

```text
Сэм, поправка: это Андрей лазил, не я.
```

The workout should be reassigned and a row should remain in `corrections` as provenance.

### Persistent history

1. Send a training note.
2. Run `docker compose restart app`.
3. Ask Sam about that training note.

The answer should use the record stored before the restart.

### Backlog

```text
Сэм, добавь в backlog: сделать нормальное сравнение недель.
```

Then:

```text
Сэм, что сейчас в backlog?
```

Speculative wording such as “когда-нибудь подключить Garmin” should normally lead to a confirmation
question instead of an immediate item.

## Inspect stored data

```powershell
docker compose exec postgres psql -U sam -d sam
```

Useful queries:

```sql
select id, name, telegram_user_id from athletes;
select id, athlete_id, direction, message_type, normalized_text from messages order by id desc limit 20;
select id, athlete_id, date, type, structured_details from workouts order by id desc;
select id, target_kind, target_id, status, payload from corrections order by id desc;
select id, type, status, title from backlog_items order by id desc;
select id, status, extracted, mutations, error from processing_runs order by id desc;
```

`DEBUG=true` enables application and SQL debug logging. It does not expose hidden model
chain-of-thought.

## Tests and checks

```powershell
python -m pip install -e ".[dev]"
python -m pytest
ruff check app tests
```

The test suite uses a temporary SQLite database for fast service-layer tests. PostgreSQL-specific
pgvector behavior is exercised when the Docker stack is running.

## Reset local data

This deletes the local Docker database volume and cannot be undone:

```powershell
docker compose down -v
```

Then run `docker compose up --build` to create a clean database and reseed the two profiles.
