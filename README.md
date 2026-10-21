# Sam

Sam is a private Telegram climbing companion for Alexey, Andrey and explicitly invited friends. This repository implements a
usable first version: text, voice and video intake, durable raw history, structured workouts and
recovery, natural-language corrections, semantic memory, an initial plan, personalized coaching,
and a local product backlog.

The product source of truth is [SPEC.md](SPEC.md).

## What is included

- one configured Telegram group with per-athlete sharing consent, and authenticated private chats;
- Telegram user ID → athlete mapping for Alexey and Andrey;
- text, voice-note, Telegram video-note and regular video processing;
- preliminary climbing-technique observations from evenly sampled video frames;
- OpenAI structured extraction and separate coaching responses;
- workouts, daily state, weight, facts, corrections and provenance;
- normalized climbing entries, evidence-gated writes and duplicate-session protection;
- deterministic 7/28/90-day analytics with daily historical snapshots;
- raw messages, episodic/coach memories and pgvector retrieval;
- source-linked current profiles, a shared tentative trip, and individually versioned plans;
- a versioned exercise/protocol catalog with equipment, venue and prerequisite filtering;
- separate program, week and day-session plans, Markdown exports and opt-in morning delivery;
- exact reported strength sets and running metrics, with explicit numeric plan confirmation;
- single-use private invitations and isolated profiles for additional athletes;
- explicit natural-language backlog creation;
- idempotent handling of repeated Telegram updates;
- a processing audit trail without hidden chain-of-thought.

Garmin screenshot extraction and GitHub Issue synchronization are not included yet. Video observations are intentionally cautious: Sam analyzes sampled frames, not
continuous motion or exact biomechanics.

## Requirements

- Docker Desktop with Docker Compose, or Python 3.12+ and PostgreSQL with pgvector;
- `ffmpeg` when running locally without Docker (the Docker image installs it automatically);
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
VIDEO_MAX_FRAMES=12
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
python -m pip install -c requirements.lock -e ".[dev]"
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
New durable facts must contain an exact evidence quote from the current user message; recent context
cannot silently create another workout. Assistant replies are NOT re-extracted as training evidence. A generated weekly plan is returned
in the same structured response that is saved and rendered to Telegram.

Workout counts, volume, grades, pain status and 7/28/90-day comparisons are calculated from canonical
database rows. The language model receives those results but does not calculate or guess them. Unknown counts,
durations and pain remain unknown. Completed grades are separate from attempted grades; French
sport grades and Fontainebleau bouldering grades are not treated as interchangeable.

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

### Video and Telegram video note

Send a regular Telegram video with the caption:

```text
Сэм, посмотри технику. Где я здесь трачу лишние силы?
```

Circular video notes are transcribed as spoken messages. For visual analysis, explicitly ask Sam
to review the clip (a reply to it works too). Requested regular videos and video documents are
sampled into frames; circular notes also supply their speech transcript. He should explicitly distinguish visible evidence
from hypotheses and must not claim precise joint loads, grip force or continuous motion analysis.

In the shared group, a bare regular video is logged but ignored until its sender explicitly asks Sam to
analyze their own climbing. Replies to another person's media are not analyzed. The hosted Telegram Bot API only lets bots download files up to 20 MB; for a larger
requested video Sam records the size and explains that a shorter or compressed copy is needed.
Casual replies to Sam do not force a response unless they contain a question or request.

### Correction

After recording a workout, send:

```text
Сэм, поправка: моя тренировка вчера длилась 45 минут, не 60.
```

If the target workout is unambiguous its source contribution is corrected, with an audit record.
Otherwise Sam asks which session. Reply to the original report or specify its date/ID.
Only the sender's own records can be corrected; workouts cannot be reassigned to a friend.

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
select workout_id, discipline, original_grade, count, completed_count from workout_entries order by id;
select athlete_id, period_end, window_days, metrics from analytics_snapshots order by period_end desc;
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
migration/restore behavior must also be tested against a separate PostgreSQL copy before release.

The September training implementation and Telegram commands are documented in
[the training guide](docs/TRAINING.md). New screenshot intake is a private review queue, not
automatic catalog publication. Catalog membership alone does not mean an exercise is ready or
appropriate for a particular athlete. Changes in this workspace are not evidence of deployment.

Opt-in synthetic training extraction and plan-generation checks (real API usage; no Telegram
sends or database writes):

```powershell
python -m app.evaluate_training
```

## Reset local data

This deletes the local Docker database volume and cannot be undone:

```powershell
docker compose down -v
```

Then run `docker compose up --build` to create a clean database and reseed the two profiles.

## Fly database backups and recovery

The production database is a single unmanaged Fly Postgres node. Volume snapshots protect against
many disk and migration failures, but they are not high availability and are not a substitute for
an off-platform SQL dump.

Inspect and create snapshots:

```powershell
fly volumes snapshots list vol_vly9dojxd99pd184 --app sam-climbing-db
fly volumes snapshots create vol_vly9dojxd99pd184 --app sam-climbing-db
```

Enable daily scheduled snapshots and retain 14 days:

```powershell
fly volumes update vol_vly9dojxd99pd184 --app sam-climbing-db --scheduled-snapshots --snapshot-retention 14
```

For recovery, stop the database machine, create a new volume from the selected snapshot with
`fly volumes create --snapshot-id <snapshot-id>`, attach it to a replacement machine at
`/var/lib/postgresql/data`, and verify row counts before starting Sam. Do not destroy the original
volume until the restored database has passed the acceptance checks above.

## Reliability and privacy contract

- Received messages and full Telegram payloads are saved before media/model processing. Edits retain
  prior text and replace the source's workout contribution; ambiguous legacy merges ask for clarification.
- Explicit session identity and source contributions distinguish follow-up details from a new session.
- Every answer and retrieved history is scoped to the requesting athlete, including in a group.
  Sam refuses requests about another athlete, comparisons, and off-topic assistant tasks.
  A person's own numeric training records can be used when they ask in a group (visible there),
  but private conversation, notes and personal explanations are not retrieved into group context.
  Topic preflight, persistent request quotas and media limits run before expensive processing;
  see `docs/TRAINING.md` for defaults and cost caveats.
- Processing and outgoing replies have separate durable retry queues (five attempts with backoff).
  The saved reply is delivered without extracting the workout again. Questions become pending only
  after delivery, expire after two days, and are scoped to athlete and chat.
- Keep **one polling machine**. Telegram sends do not have idempotency keys: a response lost after
  Telegram accepted a send can still cause a duplicate message. Delivery is retryable, not exactly-once.
- `/health` checks database reachability and polling/worker tasks and exposes queue counts, not message
  contents or credentials. Failed queue items still need an operator; there is no external pager yet.
- `/history` produces a deterministic, paginated journal; `/history 10` gets the next ten.
  Ask naturally for a weekly review or next-session advice. Weekly check-ins are not pushed on a schedule.

Review targeted historical repairs without changing data:

```powershell
fly ssh console --app sam-climbing --pty=false -C "python -m app.repair_history"
```

After inspecting the dry run and taking a backup, add `--apply`. The repair is audited and idempotent.
Do not run the legacy `app.maintenance` canonicalizer on new contribution-based records.

The worker automatically resumes received/started/retry messages after restart. To inspect terminal
failures use `python -m app.recover_pending`; add `--apply` to explicitly requeue them after fixing the
cause. Unsupported oversized media is not retried. Requeuing old replies can create delayed chat noise.

Opt-in real-model checks (uses the configured API key, costs API usage, sends nothing to Telegram):

```powershell
$env:PYTHONIOENCODING = "utf-8"
python -m app.evaluate
```

Local backups belong in ignored `.backups/`, never Git or the Docker build context. The September 12
release was tested using a custom-format database dump restored into a separate PostgreSQL database.
Snapshots and occasional local dumps are not automated off-site backups or high availability.
