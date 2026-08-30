from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date, timedelta

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Athlete, BacklogItem, DailyState, Event, Memory, Message, Plan, Workout


def _cosine(a: Iterable[float], b: Iterable[float]) -> float:
    av, bv = list(a), list(b)
    denominator = math.sqrt(sum(x * x for x in av)) * math.sqrt(sum(x * x for x in bv))
    if not denominator:
        return 0
    return sum(x * y for x, y in zip(av, bv, strict=False)) / denominator


async def recent_message_text(session: AsyncSession, athlete_id: int, limit: int = 20) -> str:
    rows = (
        await session.scalars(
            select(Message)
            .where(Message.athlete_id == athlete_id)
            .order_by(desc(Message.telegram_timestamp))
            .limit(limit)
        )
    ).all()
    rows.reverse()
    return "\n".join(
        f"{row.telegram_timestamp.isoformat()} [{row.direction}]: "
        f"{row.normalized_text or row.transcript or row.raw_text or ''}"
        for row in rows
    )


async def semantic_history(
    session: AsyncSession,
    *,
    athlete_id: int,
    query_embedding: list[float] | None,
    limit: int = 6,
) -> list[str]:
    if not query_embedding:
        return []
    if session.bind and session.bind.dialect.name == "postgresql":
        rows = (
            await session.scalars(
                select(Message)
                .where(Message.athlete_id == athlete_id, Message.embedding.is_not(None))
                .order_by(Message.embedding.cosine_distance(query_embedding))
                .limit(limit)
            )
        ).all()
    else:
        candidates = (
            await session.scalars(
                select(Message).where(
                    Message.athlete_id == athlete_id, Message.embedding.is_not(None)
                )
            )
        ).all()
        rows = sorted(
            candidates,
            key=lambda row: _cosine(row.embedding or [], query_embedding),
            reverse=True,
        )[:limit]
    return [row.normalized_text or row.transcript or row.raw_text or "" for row in rows]


class ContextBuilder:
    def __init__(self, recent_limit: int = 24):
        self.recent_limit = recent_limit

    async def build(
        self,
        session: AsyncSession,
        *,
        athlete: Athlete,
        today: date,
        current_message: str,
        semantic_matches: list[str],
        extraction_summary: str,
    ) -> str:
        workouts = (
            await session.scalars(
                select(Workout)
                .where(
                    Workout.athlete_id == athlete.id,
                    Workout.status == "active",
                    Workout.date >= today - timedelta(days=28),
                )
                .order_by(desc(Workout.date))
                .limit(20)
            )
        ).all()
        state = await session.scalar(
            select(DailyState)
            .where(DailyState.athlete_id == athlete.id)
            .order_by(desc(DailyState.date))
            .limit(1)
        )
        plan = await session.scalar(
            select(Plan)
            .where(Plan.athlete_id == athlete.id, Plan.status == "active")
            .order_by(desc(Plan.created_at))
            .limit(1)
        )
        event = await session.scalar(
            select(Event)
            .where(Event.athlete_id == athlete.id, Event.status == "active")
            .order_by(Event.date)
            .limit(1)
        )
        memories = (
            await session.scalars(
                select(Memory)
                .where(Memory.athlete_id == athlete.id, Memory.status != "retired")
                .order_by(desc(Memory.importance), desc(Memory.date))
                .limit(10)
            )
        ).all()
        backlog = (
            await session.scalars(
                select(BacklogItem)
                .where(BacklogItem.status.in_(["new", "triaged", "planned", "in_progress"]))
                .order_by(desc(BacklogItem.created_at))
                .limit(15)
            )
        ).all()
        recent = await recent_message_text(session, athlete.id, self.recent_limit)

        sections = [
            f"TODAY\n{today.isoformat()}",
            f"ATHLETE\nname={athlete.name}\nprofile={athlete.profile}",
            (
                f"TARGET EVENT\n{event.name if event else 'none'} | "
                f"{event.date if event else ''} | {event.details if event else ''}"
            ),
            f"CURRENT PLAN\n{plan.content if plan else 'No explicit plan yet.'}",
            f"LATEST DAILY STATE\n{state.date if state else ''} {state.values if state else 'none'}",
            "RECENT WORKOUTS\n"
            + "\n".join(
                f"{w.date} {w.type} duration={w.duration_minutes} rpe={w.rpe} "
                f"details={w.structured_details} notes={w.notes}"
                for w in workouts
            ),
            "ACTIVE MEMORIES\n" + "\n".join(f"[{m.kind}] {m.summary}" for m in memories),
            "SEMANTIC HISTORY MATCHES\n" + "\n".join(semantic_matches),
            "RECENT CONVERSATION\n" + recent,
            "OPEN BACKLOG\n"
            + "\n".join(f"#{b.id} [{b.type}/{b.status}] {b.title}" for b in backlog),
            f"CURRENT EXTRACTION\n{extraction_summary}",
            f"CURRENT USER MESSAGE\n{current_message}",
        ]
        return "\n\n".join(sections)
