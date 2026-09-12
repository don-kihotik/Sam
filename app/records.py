"""Evidence validation and session-level writes, independent of reply generation."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Correction, Fact, Memory, Message, Workout, WorkoutEntry
from app.grades import grade_rank, normalize_discipline, normalize_grade
from app.schemas import WorkoutCandidate


def evidence_in(text: str, quotes: list[str]) -> list[str]:
    def norm(value: str) -> str:
        return " ".join(value.casefold().split()).strip(" .,!?:;—–-\"'«»")

    source = norm(text)
    return [q for q in quotes if q and norm(q) and norm(q) in source]


NUMBER_WORDS = {
    1: r"одн\w*|один|one",
    2: r"два|две|двух|пару|two",
    3: r"три|трёх|трех|three",
    4: r"четыре|four",
    5: r"пять|five",
    6: r"шесть|six",
    7: r"семь|seven",
    8: r"восемь|eight",
    9: r"девять|nine",
    10: r"десять|ten",
}


def stated_number(text: str, value: float) -> bool:
    n = str(int(value)) if value == int(value) else str(value)
    words = NUMBER_WORDS.get(int(value), "(?!)") if value == int(value) else "(?!)"
    return bool(re.search(rf"(?<![\w.])(?:{re.escape(n)}|{words})(?![\w.])", text, re.IGNORECASE))


def validate_workout(candidate: WorkoutCandidate, text: str) -> WorkoutCandidate:
    result = candidate.model_copy(deep=True)
    result.evidence = evidence_in(text, candidate.evidence)
    if not result.evidence:
        result.present = False
        return result
    if result.duration_minutes is not None and not stated_number(text, result.duration_minutes):
        result.duration_minutes = None
    for entry in result.climbing:
        source = " ".join(evidence_in(text, entry.evidence)) or text
        entry.discipline = normalize_discipline(entry.discipline, entry.wall_style)
        original = entry.original_grade
        entry.grade_system, entry.original_grade = normalize_grade(entry.grade_system, original)
        if original and entry.original_grade is None:
            entry.notes = " ".join(filter(None, [entry.notes, original]))
        if entry.count is not None and not stated_number(source, entry.count):
            entry.count = None
        if entry.attempts is not None and not stated_number(source, entry.attempts):
            entry.attempts = None
        if entry.route_length_m is not None and not stated_number(source, entry.route_length_m):
            entry.route_length_m = None
        # A description without any quantitative climb evidence is not one completed route.
        if not entry.original_grade and not re.search(
            r"(?:\d+|три|два|две|одн\w*|one|two|three)\s*(?:трасс|маршрут|болдер|route|climb)",
            source,
            re.IGNORECASE,
        ):
            entry.count = None
    return result


def entry_record(item: dict[str, Any]) -> WorkoutEntry:
    count = item.get("count")
    complete = item.get("completed")
    return WorkoutEntry(
        discipline=normalize_discipline(item.get("discipline"), item.get("wall_style")),
        grade_system=item.get("grade_system"),
        original_grade=item.get("original_grade"),
        grade_rank=grade_rank(item.get("grade_system"), item.get("original_grade")),
        count=count,
        completed_count=(count if complete else 0) if complete is not None else None,
        attempts=item.get("attempts"),
        route_length_m=item.get("route_length_m"),
        wall_style=item.get("wall_style"),
        movement_style=item.get("movement_style"),
        notes=item.get("notes"),
    )


def rebuild_workout(workout: Workout, contributions: list[dict]) -> None:
    """Rebuild the materialized session from source contributions, including edited sources."""
    details = dict(workout.structured_details or {})
    entries: list[dict] = []
    notes, evidence, sources = [], [], []
    duration = rpe = pump = None
    pain_status = "unknown"
    pain: list = []
    for c in contributions:
        value = c["value"]
        sources.extend(c.get("sources", [c["source"]]))
        evidence.extend(value.get("evidence", []))
        if value.get("notes"):
            notes.append(value["notes"])
        if value.get("duration_minutes") is not None:
            duration = value["duration_minutes"]
        if value.get("rpe") is not None:
            rpe = value["rpe"]
        if value.get("pump") is not None:
            pump = value["pump"]
        if value.get("pain_status", "unknown") != "unknown":
            pain_status = value["pain_status"]
        if value.get("pain"):
            pain = value["pain"]
            pain_status = "reported"
        if value.get("session_action") == "correct" and value.get("climbing"):
            entries = []
        entries.extend(value.get("climbing", []))
    workout.duration_minutes = duration
    workout.rpe = rpe
    workout.pain_status = pain_status
    workout.notes = "\n".join(dict.fromkeys(notes)) or None
    workout.evidence = list(dict.fromkeys(evidence))
    workout.source_message_ids = list(dict.fromkeys(sources))
    workout.structured_details = {
        **details,
        "contributions": contributions,
        "climbing": entries,
        "pump": pump,
        "pain": pain,
    }
    workout.entries = [entry_record(e) for e in entries]
    # New sessions use explicit identity. A content fingerprint is not session identity.
    workout.fingerprint = None


async def retire_related(session: AsyncSession, athlete_id: int, source_ids: list[int]) -> None:
    rows = (
        await session.scalars(
            select(Memory).where(
                Memory.athlete_id == athlete_id, Memory.status.not_in(["retired", "contradicted"])
            )
        )
    ).all()
    for m in rows:
        if set(m.source_message_ids or []) & set(source_ids):
            m.status = "retired"
    facts = (
        await session.scalars(
            select(Fact).where(
                Fact.athlete_id == athlete_id,
                Fact.source_message_id.in_(source_ids),
                Fact.status == "active",
            )
        )
    ).all()
    for f in facts:
        f.status = "superseded"


def legacy_contribution(workout: Workout) -> dict:
    return {
        "source": workout.source_message_ids[0] if workout.source_message_ids else 0,
        "sources": list(workout.source_message_ids),
        "value": {
            "duration_minutes": workout.duration_minutes,
            "rpe": workout.rpe,
            "notes": workout.notes,
            "pain_status": workout.pain_status,
            "evidence": workout.evidence,
            **(workout.structured_details or {}),
        },
    }


async def save_session(
    session: AsyncSession,
    *,
    athlete_id: int,
    message: Message,
    candidate: WorkoutCandidate,
    source_text: str,
    today: date,
    follow_up_workout_id: int | None = None,
) -> tuple[Workout | None, str]:
    c = validate_workout(candidate, source_text)
    if not c.present:
        return None, "rejected_no_current_evidence"
    # Unknown historical dates remain unknown. "Today" is resolved by extraction.
    day = c.date or (today if c.date_precision in {"day", "exact"} else None)
    candidates = (
        await session.scalars(
            select(Workout)
            .where(Workout.athlete_id == athlete_id, Workout.status == "active")
            .order_by(Workout.created_at.desc())
        )
    ).all()
    target = (
        next((w for w in candidates if message.id in (w.source_message_ids or [])), None)
        if c.session_action != "new"
        else None
    )
    requested = c.target_workout_id or follow_up_workout_id
    if target is None and requested and c.session_action != "new":
        target = next((w for w in candidates if w.id == requested), None)
        if target is None:
            return None, "needs_session_clarification"
    if target is None and c.session_action != "new" and message.reply_to_message_id:
        parent = await session.scalar(
            select(Message).where(
                Message.telegram_chat_id == message.telegram_chat_id,
                Message.telegram_message_id == message.reply_to_message_id,
            )
        )
        if parent:
            target = next((w for w in candidates if parent.id in w.source_message_ids), None)
    if target is None and c.session_action in {"append", "correct"}:
        same_day = [w for w in candidates if day and w.date == day]
        if len(same_day) == 1:
            target = same_day[0]
        else:
            return None, "needs_session_clarification"
    if (
        target is None
        and c.session_action == "auto"
        and re.match(
            r"^\s*(?:а\s|ещ[её]|потом|затем|также|also\b|then\b|and\b)", source_text, re.IGNORECASE
        )
    ):
        for w in candidates:
            if not day or w.date != day:
                continue
            previous = await session.scalar(
                select(Message)
                .where(
                    Message.id.in_(w.source_message_ids),
                    Message.telegram_chat_id == message.telegram_chat_id,
                )
                .order_by(Message.telegram_timestamp.desc())
                .limit(1)
            )
            if previous:
                delta = message.telegram_timestamp.replace(
                    tzinfo=None
                ) - previous.telegram_timestamp.replace(tzinfo=None)
                if timedelta(0) <= delta <= timedelta(minutes=30):
                    target = w
                    break
    created = target is None
    if created:
        target = Workout(
            athlete_id=athlete_id,
            date=day,
            date_precision=c.date_precision,
            type=c.workout_type or "climbing",
            status="active",
            structured_details={},
            source_message_ids=[],
            evidence=[],
            entries=[],
        )
        session.add(target)
    contributions = list((target.structured_details or {}).get("contributions", []))
    if not contributions and target.source_message_ids:
        contributions = [legacy_contribution(target)]
    contributions = [x for x in contributions if x["source"] != message.id]
    contributions.append({"source": message.id, "value": c.model_dump(mode="json")})
    rebuild_workout(target, contributions)
    if c.session_action == "correct":
        await retire_related(session, athlete_id, target.source_message_ids)
    await session.flush()
    return target, "created" if created else "updated"


async def retract_edited_source(session: AsyncSession, message: Message) -> list[dict]:
    """Remove only the edited message's contributions before applying its new revision."""
    changes = []
    rows = (
        await session.scalars(
            select(Workout).where(
                Workout.athlete_id == message.athlete_id, Workout.status == "active"
            )
        )
    ).all()
    for w in rows:
        if message.id not in (w.source_message_ids or []):
            continue
        before = {
            "date": str(w.date),
            "details": w.structured_details,
            "sources": w.source_message_ids,
            "notes": w.notes,
        }
        contributions = (w.structured_details or {}).get("contributions")
        if contributions and any(
            message.id in c.get("sources", []) and len(c["sources"]) > 1 for c in contributions
        ):
            changes.append(
                {"kind": "correction", "status": "needs_session_clarification", "id": w.id}
            )
            continue
        if contributions:
            kept = [x for x in contributions if x["source"] != message.id]
            if kept:
                rebuild_workout(w, kept)
            else:
                w.status = "void_edited"
        elif len(w.source_message_ids) == 1:
            w.status = "void_edited"
        else:
            # Historical merged records lack per-source ownership; never erase another source.
            changes.append(
                {"kind": "correction", "status": "needs_session_clarification", "id": w.id}
            )
            continue
        session.add(
            Correction(
                athlete_id=message.athlete_id,
                source_message_id=message.id,
                target_kind="workout",
                target_id=w.id,
                status="applied",
                payload={"reason": "telegram_edit", "before": before, "revision": message.revision},
            )
        )
        changes.append({"kind": "edit", "id": w.id, "status": "applied"})
    await retire_related(session, message.athlete_id, [message.id])
    return changes
