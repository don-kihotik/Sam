# Training system implementation contract

Initial local implementation verified, 2026-09-24. Subsequent owner-authorized
deployment and completed PostgreSQL restore gate are recorded in RELEASE-2026-09-24.md.
Verification: 102 automated tests, lint, formatting and compilation passed; five
synthetic real-model scenarios passed. SQLite migration preservation and offline
PostgreSQL SQL compilation passed. At this initial checkpoint, a PostgreSQL
backup/restore rehearsal remained a deployment gate and no production changes
or Telegram test sends had been made. See the release report for final results.

## Ownership

- Catalog agent: `app/catalog.py`, `app/catalog_schemas.py`, `app/catalog_data.json`, `tests/test_catalog.py`.
- Planning/journal agent: `app/planning.py`, `app/schemas.py`, `app/records.py`, `app/analytics.py`, `tests/test_training.py`.
- Telegram/access agent: `app/telegram.py`, `app/membership.py`, `app/training_commands.py`, `app/scheduling.py`, `tests/test_training_telegram.py`.
- Coordinator: ORM models, migrations, `app/services.py`, `app/memory.py`, `app/prompts.py`, `app/ai.py`, startup/seed, integration tests, documentation and review.

Do not edit another owner's files. Propose interface changes to the coordinator. No deployments, commits, production writes, or external messages from subagents. Preserve existing tests and behavior except explicitly changed requirements.

## Existing model additions (coordinator owns)

- Plan: scope (`program`, `week`, `session`, default `week`), parent_id, supersedes_id. Immutable versions via new rows. Existing start_date/end_date/content/status/visibility remain.
- Workout: nullable plan_id; actual details remain source-contribution based in structured_details.
- TrainingPreference: athlete_id PK; profile JSON; private_chat_id/shared_chat_id; morning_time nullable HH:MM; catalog_mode; log_plan_id/log_workout_id/log_expires_at.
- Invitation: token_hash, created_by, expires_at, redeemed_by Telegram user ID.
- CatalogProtocol: slug/version unique; title/category/status/specification JSON.
- CatalogSource: athlete_id, source_message_id, file_unique_id (unique per athlete), album_id, protocol_id nullable, status, extracted JSON. Raw sources stay owner-private.
- Outbox: nullable source_message_id/reply_to_message_id; dedupe_key unique nullable; payload JSON.

## Shared interfaces

Catalog (`app.catalog`):

- `async seed_catalog(session) -> None`: idempotent built-in normalized inventory, never workouts. No commit.
- `async catalog_context(session, *, profile: dict, limit: int = 20) -> list[dict]`: only ready, applicable protocols; omit private source IDs/text. Deterministic equipment/prerequisite filtering; unknown prerequisites must not be assumed met. Include IDs and version plus parameters.
- `async validate_blocks(session, blocks: list[dict], *, profile: dict) -> list[dict]`: return frozen normalized blocks with protocol snapshots; reject unknown/unready/ineligible IDs and unjustified changes with ValueError. Ad hoc noncatalog blocks explicitly labelled, not source prescriptions.
- `async receive_catalog_source(session, *, message, extracted: dict | None = None) -> object`: stage photo source with album/dedupe, no publication and no workout; no commit. Photo analysis hook may be added by coordinator.

Planning (`app.planning`):

- `async save_plan(session, *, athlete_id: int, candidate, source_message_id: int, evidence: list[str], today: date, visibility: str = 'private') -> Plan`: validate owner/parent/scope/date/protocols, supersede only relevant scope/target, materialize dated weekly child sessions. No commit.
- `render_training_plan(plan: Plan) -> str`: deterministic readable Telegram text. Same content used for MD export.
- `async planning_context(session, *, athlete_id: int, today: date, public: bool = False) -> dict`: bounded active program/week/day, IDs and versions.
- `async latest_session_plan(session, *, athlete_id: int, day: date) -> Plan | None`.
- `async begin_log(session, *, athlete_id: int, plan_id: int, now: datetime) -> str`: owner check, persist bounded log context, return exact planned parameters to confirm or change; never write actual numbers from mere completion.

Telegram/access:

- `async ensure_preference(session, athlete) -> TrainingPreference`: default private-only new users. Legacy sharing explicitly seeded by coordinator.
- `async redeem_invitation(session, *, token: str, telegram_user_id: int, display_name: str, chat_id: int, now: datetime) -> Athlete`: private chat only; atomic one-time, expiry check; no copied goals/history.
- `async handle_training_command(session, *, athlete, message, settings, today: date) -> CommandResult | None`, CommandResult fields `text: str`, `payload: dict` (default empty), optional `model_request: str | None`. None means normal model flow; model_request passes a natural request onward with author context. Explicit command failures return readable text, not exceptions.
- Private-only commands: /setup, /invite (admin only), /today, /week, /program, /session ID, /log ID, /export ID, /morning HH:MM|off, /timezone IANA, /sharing on|off, /catalog [on|off]. Stable IDs and ownership checks on every action.
- Payload format: `buttons`: rows of `{text, data}`; callback data `train:<action>:<id>`. Optional `document`: `{filename, text}` for UTF-8 MD. No arbitrary file paths/URLs.
- Callbacks use same authenticated/durable processing path as commands. Handler must validate sender and scope, check replay idempotency, map action to supported command; never trust a displayed name or blindly execute callback data as text.
- `async enqueue_morning_plans(session, *, now: datetime) -> int`: opt-in timezone-aware, only today's active session plans, persisted dedupe keys; no backlog blast after restart. No commit. Uses render_training_plan, Outbox with null source/reply and payload buttons. Delivery rechecks current plan/opt-in to suppress stale queued reminders.

## Non-negotiable data semantics

Chat/voice/buttons all feed the same actual record: exact climbs, grades, attempts, per-exercise sets/reps/weight and running duration/distance. Planned values become actual ONLY after explicit confirmation of shown numbers. 'Done' is not that confirmation. Unknown is not zero. Corrections replace contributions without duplicating sessions. Plan IDs, source IDs and workout IDs require ownership validation.

Catalog classifications preserve original category and distinguish inferred tags, applicability level vs intensity, mandatory/alternative equipment, duration assumptions, side/grip/load baseline. Never auto-publish conflicting or partial protocols. Saved snapshot/version must survive later catalog edits.

All new user data private by default; existing Alexey/Andrey sharing is explicit and limited to configured group. Personal context/notes/private source images never enter shared catalog or another user's model context. No third-athlete hardcoding.

## Verification

### Policy amendment: self-only training assistant (2026-09-24)

The owner's latest rule supersedes earlier shared-training access: every answer,
history lookup and model context is scoped to the requesting athlete, even in a
group. Group participation is not permission to disclose another athlete's
records, compare athletes, or log/correct records for a friend. Personal context
remains private. General climbing/training questions remain allowed; unrelated
or mixed assistant requests receive a fixed refusal. Durable per-revision quotas
cover classification, voice/video/OCR and plan-generation commands. Classification
is not a database authorization mechanism; ownership is independently enforced.
See `TRAINING.md` for limits, paid-media caveats, and the single-worker assumption.

Run targeted tests via `.venv/Scripts/python.exe -m pytest ...`. Add tests for cross-user IDs, duplicate updates/callbacks, partial logs, corrections, unknown parameters, program/week/day coexistence, timezone/reminder dedupe, catalog filters and unsafe sources. Coordinator performs full-suite review and migration rehearsal before any deployment.
