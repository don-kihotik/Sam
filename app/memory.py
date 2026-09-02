from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date, timedelta

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.analytics import analytics_windows
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
    exclude_message_id: int | None = None,
) -> list[str]:
    if not query_embedding:
        return []
    filters = [Message.athlete_id == athlete_id, Message.embedding.is_not(None)]
    if exclude_message_id is not None:
        filters.append(Message.id != exclude_message_id)
    if session.bind and session.bind.dialect.name == "postgresql":
        rows = (
            await session.scalars(
                select(Message)
                .where(*filters)
                .order_by(Message.embedding.cosine_distance(query_embedding))
                .limit(limit)
            )
        ).all()
    else:
        candidates = (await session.scalars(select(Message).where(*filters))).all()
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
        memory_audit = any(
            marker in current_message.casefold()
            for marker in (
                "что ты помнишь",
                "проверь память",
                "все тренировки",
                "сколько раз я лазил",
                "истори",
                "аналитик",
                "статистик",
            )
        )
        workout_query = (
            select(Workout)
            .options(selectinload(Workout.entries))
            .where(Workout.athlete_id == athlete.id, Workout.status == "active")
            .order_by(Workout.date.desc().nullslast(), desc(Workout.created_at))
        )
        if not memory_audit:
            workout_query = workout_query.where(
                or_(Workout.date >= today - timedelta(days=28), Workout.date.is_(None))
            ).limit(20)
        workouts = (await session.scalars(workout_query)).all()
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
            .order_by(Event.date.asc().nullslast(), desc(Event.created_at))
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
        analytics = await analytics_windows(session, athlete.id, today)

        def workout_line(workout: Workout) -> str:
            entries = ", ".join(
                f"{entry.count}×{entry.original_grade or '?'} ({entry.discipline or 'unknown'}; "
                f"completed={entry.completed_count if entry.completed_count is not None else 'unknown'})"
                for entry in workout.entries
            )
            return (
                f"id={workout.id} date={workout.date or 'unknown'} precision={workout.date_precision} "
                f"type={workout.type} duration={workout.duration_minutes} rpe={workout.rpe} "
                f"pump={(workout.structured_details or {}).get('pump')} "
                f"pain_status={workout.pain_status} entries=[{entries}] notes={workout.notes} "
                f"sources={workout.source_message_ids}"
            )

        sections = [
            f"TODAY\n{today.isoformat()}",
            f"ATHLETE\nname={athlete.name}\nprofile={athlete.profile}",
            (
                f"TARGET EVENT\n{event.name if event else 'none'} | "
                f"{event.date if event else ''} | precision={event.date_precision if event else ''} | "
                f"{event.details if event else ''}"
            ),
            f"CURRENT PLAN\n{plan.content if plan else 'No explicit plan yet.'}",
            f"DETERMINISTIC ANALYTICS (database-calculated; never estimate)\n{analytics}",
            f"LATEST DAILY STATE\n{state.date if state else ''} {state.values if state else 'none'}",
            "RECENT WORKOUTS\n" + "\n".join(workout_line(w) for w in workouts),
            "ACTIVE MEMORIES\n" + "\n".join(f"[{m.kind}] {m.summary}" for m in memories),
            "SEMANTIC HISTORY MATCHES\n" + "\n".join(semantic_matches),
            "RECENT CONVERSATION\n" + recent,
            "OPEN BACKLOG\n"
            + "\n".join(f"#{b.id} [{b.type}/{b.status}] {b.title}" for b in backlog),
            f"VALIDATED EXTRACTION RESULT\n{extraction_summary}",
            f"CURRENT USER MESSAGE\n{current_message}",
            (
                "RELIABILITY RULES\nUse canonical workouts, active event/plan and deterministic analytics "
                "as the source of truth. Never turn pain_status=unknown into no pain. For memory/history "
                "questions, enumerate only the canonical records above and explicitly say what is unknown. "
                "A candidate marked rejected in mutation_results is not a fact and must not be claimed."
            ),
        ]
        return "\n\n".join(sections)
