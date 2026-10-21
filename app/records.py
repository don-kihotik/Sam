"""Evidence validation and session-level writes, independent of reply generation."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Correction, Fact, Memory, Message, Plan, Workout, WorkoutEntry
from app.grades import grade_rank, normalize_discipline, normalize_grade
from app.schemas import ActualActivityCandidate, WorkoutCandidate


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


def stated_measurement(text: str, value: float, unit: str) -> bool:
    """Match a base-unit value against a spoken/written unit, including km→m."""

    aliases = {
        "kg": ((1.0, r"(?:kg|кг|килограмм(?:а|ов)?)"),),
        "m": ((1.0, r"(?:m|метр(?:а|ов)?|м)"),),
        "km": ((1000.0, r"(?:km|км|километр(?:а|ов)?)"),),
        "seconds": (
            (1.0, r"(?:s|sec(?:onds?)?|сек(?:унд(?:ы|у|а)?)?)"),
            (60.0, r"(?:min(?:utes?)?|мин(?:ут(?:ы|у|а)?)?)"),
        ),
        "minutes": (
            (1.0, r"(?:min(?:utes?)?|мин(?:ут(?:ы|у|а)?)?)"),
            (60.0, r"(?:h|hours?|час(?:а|ов)?)"),
        ),
    }
    for scale, suffix in aliases.get(unit, ()):  # only known units can validate a fact
        values = re.findall(rf"(?<![\w.])(\d+(?:[.,]\d+)?)\s*{suffix}(?!\w)", text, re.IGNORECASE)
        # Russian reports often say "минут 40" rather than "40 минут".
        values += re.findall(rf"{suffix}\s*(\d+(?:[.,]\d+)?)(?!\w)", text, re.IGNORECASE)
        for raw in values:
            if abs(float(raw.replace(",", ".")) * scale - float(value)) < 0.0001:
                return True
    if unit == "m":
        return stated_measurement(text, value, "km")
    return False


def _validated_activities(candidate: WorkoutCandidate, text: str) -> list[ActualActivityCandidate]:
    """Retain only values that have their own source evidence.

    A plan can be shown in the UI, but it is deliberately unavailable here: this
    function only receives words supplied in the current report/confirmation.
    """

    activities: list[ActualActivityCandidate] = []
    for activity in candidate.activities:
        item = activity.model_dump(mode="json")
        matched = evidence_in(text, activity.evidence or candidate.evidence)
        source = " ".join(matched)
        # Explicit activity evidence is required before anything becomes a fact.
        if not source:
            continue
        else:
            if item.get("duration_minutes") is not None and not stated_measurement(
                source, item["duration_minutes"], "minutes"
            ):
                item["duration_minutes"] = None
            if item.get("distance_m") is not None and not stated_measurement(
                source, item["distance_m"], "m"
            ):
                item["distance_m"] = None
            validated_sets = []
            for set_item in item.get("sets", []):
                set_source = " ".join(evidence_in(text, set_item.get("evidence", []))) or source
                if not set_source:
                    continue
                for key, unit in (
                    ("set_number", None),
                    ("reps", None),
                    ("weight_kg", "kg"),
                    ("duration_seconds", "seconds"),
                    ("distance_m", "m"),
                ):
                    if set_item.get(key) is None:
                        continue
                    stated = (
                        stated_measurement(set_source, set_item[key], unit)
                        if unit
                        else stated_number(set_source, set_item[key])
                    )
                    if not stated:
                        set_item[key] = None
                set_item["evidence"] = evidence_in(text, set_item.get("evidence", [])) or matched
                # Don't retain an empty anonymous set merely because a protocol had one.
                if any(
                    set_item.get(key) is not None
                    for key in ("reps", "weight_kg", "duration_seconds", "distance_m")
                ):
                    validated_sets.append(set_item)
            item["sets"] = validated_sets
            item["evidence"] = matched
        activities.append(ActualActivityCandidate.model_validate(item))
    return activities


def validate_workout(candidate: WorkoutCandidate, text: str) -> WorkoutCandidate:
    result = candidate.model_copy(deep=True)
    result.evidence = evidence_in(text, candidate.evidence)
    if not result.evidence:
        result.present = False
        return result
    if result.duration_minutes is not None and not stated_measurement(
        text, result.duration_minutes, "minutes"
    ):
        result.duration_minutes = None
    result.activities = _validated_activities(result, text)
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
    activities: list[dict] = []
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
        if value.get("session_action") == "correct" and value.get("activities"):
            corrected = {
                (
                    activity.get("kind"),
                    activity.get("exercise") or activity.get("title"),
                    activity.get("condition"),
                )
                for activity in value["activities"]
            }
            activities = [
                activity
                for activity in activities
                if (
                    activity.get("kind"),
                    activity.get("exercise") or activity.get("title"),
                    activity.get("condition"),
                )
                not in corrected
            ]
        entries.extend(value.get("climbing", []))
        activities.extend(value.get("activities", []))
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
        "activities": activities,
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
    if c.plan_id is not None:
        plan = await session.get(Plan, c.plan_id)
        if plan is None or plan.athlete_id != athlete_id or plan.scope != "session":
            return None, "needs_plan_clarification"
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
    # One concrete session plan maps to at most one active actual workout for its
    # day.  This prevents repeated voice/text reports from creating duplicates.
    if target is None and c.plan_id is not None:
        planned = [
            w for w in candidates if w.plan_id == c.plan_id and (day is None or w.date == day)
        ]
        if len(planned) == 1:
            target = planned[0]
        elif len(planned) > 1:
            return None, "needs_session_clarification"
    # An explicit plan is a stronger identity than date.  Never merge it into
    # an unrelated same-day run/climb simply because that is the only row.
    if target is None and c.plan_id is None and c.session_action in {"append", "correct"}:
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
    if target is not None and c.plan_id is not None:
        if target.plan_id not in (None, c.plan_id):
            return None, "needs_plan_clarification"
        # Only an explicit requested workout may be linked retroactively.  A
        # reply/date heuristic should not silently reclassify an old session.
        if target.plan_id is None and (
            c.target_workout_id is not None or follow_up_workout_id is not None
        ):
            target.plan_id = c.plan_id
    if created:
        target = Workout(
            athlete_id=athlete_id,
            plan_id=c.plan_id,
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
    contributions.append(
        {"source": message.id, "sources": [message.id], "value": c.model_dump(mode="json")}
    )
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
