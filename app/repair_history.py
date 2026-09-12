"""Audited, idempotent repairs for the reviewed production incidents. Dry run by default."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.analytics import refresh_analytics_snapshots
from app.config import get_settings
from app.db.models import (
    AnalyticsSnapshot,
    Athlete,
    Correction,
    Event,
    Fact,
    Memory,
    Message,
    Plan,
    Workout,
)
from app.db.session import Database
from app.grades import grade_rank, normalize_discipline, normalize_grade
from app.records import legacy_contribution, rebuild_workout, retire_related


async def repair(session) -> list[dict]:
    changes = []
    messages = {m.id: m for m in (await session.scalars(select(Message))).all()}
    athletes = {a.name: a for a in (await session.scalars(select(Athlete))).all()}
    existing = (await session.scalars(select(Correction))).all()
    applied = {c.payload.get("repair_key") for c in existing}

    def audit(key, row, source, before):
        session.add(
            Correction(
                athlete_id=row.athlete_id,
                source_message_id=source,
                target_kind="workout" if isinstance(row, Workout) else "memory",
                target_id=row.id,
                status="applied",
                payload={"repair_key": key, "before": before},
            )
        )
        changes.append({"repair": key, "id": row.id})

    # Only memories based entirely on shared-chat sources are made group-visible.
    for model in (Memory, Fact, Plan, Event):
        for row in (await session.scalars(select(model))).all():
            ids = [row.source_message_id] if isinstance(row, Fact) else row.source_message_ids
            if ids and all(i in messages and messages[i].telegram_chat_id < 0 for i in ids):
                row.visibility = "group"

    workouts = (await session.scalars(select(Workout).where(Workout.status == "active"))).all()
    repair_source = next(
        (m for m in messages.values() if m.telegram_message_id == -2026091202), None
    )
    if repair_source is None:
        alexey = athletes.get("Alexey")
        if alexey:
            repair_source = Message(
                telegram_message_id=-2026091202,
                telegram_chat_id=get_settings().telegram_allowed_chat_id or -1,
                athlete_id=alexey.id,
                direction="configuration",
                message_type="configuration",
                telegram_timestamp=datetime(2026, 9, 12, tzinfo=UTC),
                raw_text="Reviewed user-provided evidence: Alexey baseline pump was 6–7/10, not exactly 6.5. Andrey's screenshot says: Пролез пару раз 5с с нижней страховкой.",
                attachment_data={
                    "origin": "user_provided_spec_and_screenshot",
                    "screenshot": "codex-clipboard-9ddad0ed-4009-4883-a7d1-7e6416992a85.png",
                },
            )
            session.add(repair_source)
            await session.flush()
    for w in workouts:
        sources = [messages[i] for i in w.source_message_ids if i in messages]
        words = " ".join(m.raw_text or m.transcript or m.normalized_text or "" for m in sources)
        if (
            repair_source
            and w.date is None
            and w.structured_details.get("date_label") == "August 2026"
            and "baseline-pump-range" not in applied
        ):
            audit("baseline-pump-range", w, repair_source.id, {"details": w.structured_details})
            w.structured_details = {**w.structured_details, "pump": None, "pump_range": [6, 7]}
        if (
            repair_source
            and w.structured_details.get("recovered_from_screenshot")
            and "Пролез пару раз с нижней страховкой" in words
            and "andrey-screenshot-5c" not in applied
        ):
            audit(
                "andrey-screenshot-5c",
                w,
                repair_source.id,
                {"details": w.structured_details, "sources": w.source_message_ids},
            )
            for e in w.entries:
                e.grade_system, e.original_grade = "French", "5c"
            w.structured_details = {
                **w.structured_details,
                "climbing": [
                    {
                        "discipline": "lead",
                        "grade_system": "French",
                        "original_grade": "5c",
                        "count": 2,
                        "completed": True,
                    }
                ],
            }
            w.source_message_ids = [*w.source_message_ids, repair_source.id]
        if "легкие трассы, минут 40 после работы" in words and "easy-count" not in applied:
            audit("easy-count", w, sources[0].id, {"details": w.structured_details})
            for e in w.entries:
                e.count, e.completed_count, e.original_grade, e.grade_rank = None, None, None, None
            details = dict(w.structured_details)
            details["climbing"] = [
                {
                    "discipline": "bouldering",
                    "count": None,
                    "completed": None,
                    "notes": "easy bouldering",
                }
            ]
            w.structured_details = details
        for e in w.entries:
            e.discipline = normalize_discipline(e.discipline, e.wall_style)
            e.grade_system, e.original_grade = normalize_grade(e.grade_system, e.original_grade)
            e.grade_rank = grade_rank(e.grade_system, e.original_grade)
            if "5ки 3 шт пролез по 17 метров" in words and e.original_grade == "5":
                e.route_length_m = 17
    andrey = athletes.get("Andrey")
    if andrey and "andrey-sept5-session" not in applied:
        same = [w for w in workouts if w.athlete_id == andrey.id and w.date == date(2026, 9, 5)]
        first = second = None
        for w in same:
            words = " ".join(
                (messages[i].raw_text or "") for i in w.source_message_ids if i in messages
            )
            if "6а попробовал" in words or "6a попробовал" in words:
                first = w
            if "5ки 3 шт" in words:
                second = w
        if first and second and first.id != second.id:
            before = {
                "first": first.structured_details,
                "second": second.structured_details,
                "first_sources": first.source_message_ids,
                "second_sources": second.source_message_ids,
            }
            a, b = legacy_contribution(first), legacy_contribution(second)
            for entry in b["value"].get("climbing", []):
                entry.update(grade_system="French", original_grade="5", route_length_m=17)
            rebuild_workout(first, [a, b])
            second.status = "void_duplicate"
            await retire_related(session, andrey.id, second.source_message_ids)
            audit("andrey-sept5-session", first, second.source_message_ids[0], before)
    for m in (await session.scalars(select(Memory).where(Memory.status != "retired"))).all():
        words = " ".join(
            (messages[i].raw_text or "") for i in m.source_message_ids if i in messages
        )
        if "теперь не отвертишься" in words and f"banter-memory-{m.id}" not in applied:
            audit(
                f"banter-memory-{m.id}",
                m,
                m.source_message_ids[0],
                {"summary": m.summary, "status": m.status},
            )
            m.status = "retired"
    # Recompute ranks, entries and snapshots after corrections; no messages are deleted.
    await session.flush()
    for athlete in athletes.values():
        dates = set(
            (
                await session.scalars(
                    select(AnalyticsSnapshot.period_end).where(
                        AnalyticsSnapshot.athlete_id == athlete.id
                    )
                )
            ).all()
        )
        dates.add(datetime.now(ZoneInfo(athlete.timezone)).date())
        for period_end in dates:
            await refresh_analytics_snapshots(session, athlete.id, period_end)
    return changes


async def main(apply: bool) -> None:
    db = Database(get_settings().database_url)
    async with db.sessions() as session:
        changes = await repair(session)
        if apply:
            await session.commit()
        else:
            await session.rollback()
        print(json.dumps({"applied": apply, "repairs": changes}))
    await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))
