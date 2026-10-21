"""Durable, opt-in morning session reminder enqueueing."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Athlete, Outbox, TrainingPreference
from app.planning import latest_session_plan, render_training_plan


def _valid_time(value: str | None) -> tuple[int, int] | None:
    if not value or len(value) != 5 or value[2] != ":":
        return None
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except ValueError:
        return None
    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


async def enqueue_morning_plans(session: AsyncSession, *, now: datetime) -> int:
    """Queue due local-day plans once, without a restart backlog.

    A reminder is due in a bounded 30-minute local grace window. This permits a
    short restart catch-up without an old-day or all-history reminder blast.
    """
    now = now.astimezone(UTC)
    rows = (
        await session.execute(
            select(Athlete, TrainingPreference)
            .join(TrainingPreference, TrainingPreference.athlete_id == Athlete.id)
            .where(Athlete.active.is_(True), TrainingPreference.morning_time.is_not(None))
        )
    ).all()
    created = 0
    for athlete, preference in rows:
        clock = _valid_time(preference.morning_time)
        if clock is None or preference.private_chat_id is None:
            continue
        try:
            local_now = now.astimezone(ZoneInfo(athlete.timezone))
        except ZoneInfoNotFoundError:
            continue
        scheduled_minute = clock[0] * 60 + clock[1]
        current_minute = local_now.hour * 60 + local_now.minute
        # A short same-day grace window makes a process restart harmless without
        # sending an old day's reminder in a batch.
        if not 0 <= current_minute - scheduled_minute <= 30:
            continue
        plan = await latest_session_plan(session, athlete_id=athlete.id, day=local_now.date())
        if plan is None or plan.status != "active":
            continue
        key = f"morning:{athlete.id}:{local_now.date().isoformat()}"
        already = await session.scalar(select(Outbox.id).where(Outbox.dedupe_key == key))
        if already is not None:
            continue
        text = "План на сегодня:\n\n" + render_training_plan(plan)
        session.add(
            Outbox(
                source_message_id=None,
                dedupe_key=key,
                payload={
                    "reminder_plan_id": plan.id,
                    "reminder_day": local_now.date().isoformat(),
                    "buttons": [
                        [
                            {"text": "Открыть сессию", "data": f"train:session:{plan.id}"},
                            {"text": "Начать лог", "data": f"train:log:{plan.id}"},
                        ]
                    ],
                },
                athlete_id=athlete.id,
                telegram_chat_id=preference.private_chat_id,
                reply_to_message_id=None,
                text=text,
            )
        )
        created += 1
    await session.flush()
    return created
