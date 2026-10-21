# Release 2026-10-21 — planning task continuity

## User-facing change

Sam now keeps an unfinished planning request as structured private state. A
short answer to a clarification question continues the original task instead
of being interpreted as a new standalone workout request.

For example, the sequence below produces one month-long program rather than a
single session:

1. `собери мне детальный план тренировок на ближайший месяц`
2. `три тренировки в неделю ... начну в это воскресенье ...`

The task state is scoped by athlete and chat, expires after seven days, is not
included in the shareable athlete profile, and is completed only after the
requested plan scope is successfully saved.

## Guardrails

- A monthly request cannot silently collapse into a session plan.
- The start date is checked against explicit relative-date intent.
- A detailed monthly program needs dates, phases, and descriptive weekly
  activities; generic placeholder labels are rejected and repaired.
- Russian requests receive Russian athlete-facing plan content.
- Illness restrictions remain strict for near-term sessions without banning
  appropriate later progression inside a month program.
- Program schedule templates no longer create shallow first-week session
  records; explicitly dated, fully specified sessions are still saved.

## Verification

- `python -m ruff check app tests`
- `python -m pytest -q`: 171 passed
- Real-model exact two-turn monthly-program replay: passed
- Real-model seven-turn session create/shorten/reschedule replay: passed

