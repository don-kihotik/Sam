from __future__ import annotations

import argparse
import asyncio
import json
import re
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai import AIService
from app.analytics import grade_rank, refresh_analytics_snapshots
from app.config import get_settings
from app.db.models import DailyState, Memory, Message, Workout, WorkoutEntry
from app.db.session import Database

_CONFIRMATION = re.compile(
    r"(?:ты\s+(?:это\s+)?записал|запомнил|сохранил|did you (?:save|record|remember))",
    re.IGNORECASE,
)


def fingerprint_existing(workout: Workout) -> str:
    details = workout.structured_details or {}

    def normalized_discipline(item: dict) -> str | None:
        discipline = (item.get("discipline") or "").casefold().replace(" ", "_")
        wall_style = (item.get("wall_style") or "").casefold().replace(" ", "_")
        if "auto" in discipline or "auto" in wall_style:
            return "auto_belay"
        if "rope" in discipline:
            return "roped_climbing"
        if "boulder" in discipline:
            return "bouldering"
        return discipline or None

    climbing = sorted(
        (
            normalized_discipline(item),
            item.get("grade_system")
            or ("V" if str(item.get("original_grade", "")).upper().startswith("V") else None),
            item.get("original_grade"),
            item.get("count", 1),
            item.get("attempts"),
        )
        for item in details.get("climbing", [])
    )
    payload = {
        "athlete": workout.athlete_id,
        "date": workout.date.isoformat() if workout.date else None,
        "duration": workout.duration_minutes,
        "rpe": workout.rpe,
        "pump": details.get("pump"),
        "climbing": climbing,
    }
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def canonicalize_history(session: AsyncSession) -> dict[str, int]:
    workouts = (
        await session.scalars(
            select(Workout)
            .options(selectinload(Workout.entries))
            .where(Workout.status == "active")
            .order_by(Workout.created_at)
        )
    ).all()
    populated = 0
    retired = 0
    duplicate_sources: set[int] = set()
    by_fingerprint: dict[str, Workout] = {}
    for workout in workouts:
        details = workout.structured_details or {}
        if workout.pain_status == "unknown" and details.get("pain"):
            workout.pain_status = "reported"
        if not workout.entries:
            for item in details.get("climbing", []):
                completed = item.get("completed")
                session.add(
                    WorkoutEntry(
                        workout_id=workout.id,
                        discipline=item.get("discipline"),
                        grade_system=item.get("grade_system"),
                        original_grade=item.get("original_grade"),
                        grade_rank=grade_rank(item.get("grade_system"), item.get("original_grade")),
                        count=item.get("count", 1),
                        completed_count=(item.get("count", 1) if completed is True else 0)
                        if completed is not None
                        else None,
                        attempts=item.get("attempts"),
                        wall_style=item.get("wall_style"),
                        movement_style=item.get("movement_style"),
                        notes=item.get("notes"),
                    )
                )
            populated += 1
        if workout.fingerprint is None:
            workout.fingerprint = fingerprint_existing(workout)
        canonical = by_fingerprint.get(workout.fingerprint)
        if canonical is None:
            by_fingerprint[workout.fingerprint] = workout
            continue
        source_messages = (
            await session.scalars(select(Message).where(Message.id.in_(workout.source_message_ids)))
        ).all()
        if not any(
            _CONFIRMATION.search(
                message.normalized_text or message.transcript or message.raw_text or ""
            )
            for message in source_messages
        ):
            continue
        duplicate_sources.update(workout.source_message_ids)
        canonical.source_message_ids = list(
            dict.fromkeys([*canonical.source_message_ids, *workout.source_message_ids])
        )
        canonical.evidence = list(dict.fromkeys([*canonical.evidence, *workout.evidence]))
        workout.status = "void_duplicate"
        retired += 1

    if duplicate_sources:
        memories = (await session.scalars(select(Memory).where(Memory.status != "retired"))).all()
        for memory in memories:
            if duplicate_sources.intersection(memory.source_message_ids):
                memory.status = "retired"
        states = (await session.scalars(select(DailyState))).all()
        for state in states:
            state.source_message_ids = [
                source for source in state.source_message_ids if source not in duplicate_sources
            ]

    athlete_ids = {workout.athlete_id for workout in workouts}
    for athlete_id in athlete_ids:
        dated = [w.date for w in workouts if w.athlete_id == athlete_id and w.date is not None]
        if dated:
            await refresh_analytics_snapshots(session, athlete_id, max(dated))
    await session.flush()
    return {"normalized_workouts": populated, "retired_duplicates": retired}


async def backfill_outgoing_embeddings(session: AsyncSession, ai: AIService) -> int:
    messages = (
        await session.scalars(
            select(Message).where(
                Message.direction == "outgoing",
                Message.embedding.is_(None),
                Message.normalized_text.is_not(None),
            )
        )
    ).all()
    completed = 0
    for message in messages:
        message.embedding = await ai.embed(message.normalized_text or "")
        completed += message.embedding is not None
    await session.flush()
    return completed


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--embeddings", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    database = Database(settings.database_url)
    async with database.sessions() as session:
        result = await canonicalize_history(session)
        if args.embeddings:
            result["outgoing_embeddings"] = await backfill_outgoing_embeddings(
                session, AIService(settings)
            )
        await session.commit()
        print(json.dumps(result))
    await database.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
