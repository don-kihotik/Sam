# Coaching quality release — 24 September 2026

## Scope

- Deterministic local session date and same-conversation, self-only plan linkage.
- One session-plan writer per coached message; immutable revisions, bounded one-attempt repair.
- Athlete-scoped validation diagnostics. Invalid proposals are not shown as accepted plans.
- Effective equipment profile includes evidenced own facts and transient session restrictions;
  no new durable availability inferred from a single Sunday.
- Compact Russian cards with executable catalog steps, detailed Markdown export unchanged.
- Shared duration calculation includes fixed catalog warmups. Explicit session budgets are
  validated before saving and carried through later revisions, including date changes.
- Catalog dose changes belong in validated parameters, not contradictory numerical prose.
- Recent explicitly reported illness is a conservative conversation-scoped signal, not a
  medical assessment. It blocks non-warmup/non-technique catalog selection while unresolved.
- Future-health questions become current-health questions; pending equivalent questions are
  not repeatedly asked. No-op extractor plans ignore unused nested containers.

Prompt version: `v9-coaching-quality`. Database schema remains `20260924_0004`.
Self-only v8 conversation remains readable; older potentially cross-athlete context stays excluded.

## Verification and backup

- Full suite reached 167 passing tests; Ruff and compile checks passed.
- `python -m app.evaluate_dialogue --show-replies` exercises the seven-turn September 24
  conversation in disposable SQLite with the configured live models. Telegram delivery is
  disabled, synthetic identities are used, and production records are not modified.
- Earlier real-model runs found duplicates, stale targets, contradictory catalog dosing,
  repeated questions, an irrelevant no-op schema failure, and ignored total time limits.
  These failures were used to add regression checks; they are not counted as passing runs.
- Final seven-turn live-model replay passed: one active session, correct Sunday September 27,
  a verified 30-minute whole-session plan, and October 2 rescheduling retaining that budget.
  Two turns needed the bounded structured-plan repair. No test workouts or replies were
  sent to production. This is a functional scenario pass, not a claim of perfect prose.
- Pre-release dump: `.backups/sam-pre-quality-20260924.dump`, 871073 bytes.
- SHA256: `2A46E28AE4C7539D1373CCA0544BB6F00209A63E8941F9009D85407E3A7FF122`.
- Rollback image: `registry.fly.io/sam-climbing:deployment-01M3AGB1BF1YVM7VYKRNM09D5F`.
  No schema downgrade is required for this quality release.

## Boundaries

The calendar and readiness recognizers are deliberately bounded, not general natural-language
understanding or medical triage. Unsupported/ambiguous dates should be clarified. Equipment
evidence does not confer competence or physical readiness. Tests do not demonstrate perfect
model behavior on arbitrary conversations. No historical failed media jobs are replayed.

## Deployed and checked

- Final full suite: 167 passed; Ruff clean.
- Fly app `sam-climbing`, machine `84711df2752452`, version 19, region `yyz`.
- Image `registry.fly.io/sam-climbing:deployment-01M3AJG8A29QF3WV7Z978BRKJG`.
- Digest `sha256:110c0758b4363f6c7386fa36e2a99cdca46c76f55f7bc8ec3d7fbb34eef01675`.
- Immediate replacement completed around 20:43:38 UTC. One service check passing;
  HTTP health confirms polling/worker running, DB healthy, no current error, no queued
  messages and no unsent replies. Existing 159 historical failures remain unchanged.
- Read deployed `PROMPT_VERSION`: `v9-coaching-quality`; schema stays `20260924_0004`.
- Repaired the one reviewed wrong-date session through normal immutable versioning:
  plan 5 (Monday September 28, source message 445) superseded by plan 6 (Sunday September 27).
  Original source text and ownership/date preconditions were checked in a dry run before
  applying. Four blocks retained; adaptation reason records the calendar bug. No Telegram
  response was emitted by this repair. The old plan is retained for audit, not deleted.
- Counts before/after: 12 workouts, 29 workout entries, 447 messages. No synthetic
  production training data was created. No Git commit or push was made.
- Fly CLI machine-selection SSH calls briefly stalled during replacement; addressing the
  observed VM address directly succeeded. An initial health request during replacement
  timed out; subsequent health and service checks passed.

No user-originated Telegram message was sent during the deployment smoke check. The live
seven-turn conversation was exercised separately in the disposable test database.
