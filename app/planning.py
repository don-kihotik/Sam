"""Versioned training plans and deliberately low-friction log prompts.

Plans are prescriptions.  This module never turns a prescription into an actual
workout; records.py is the sole writer of performed metrics.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Athlete, Message, Plan, TrainingPreference, Workout
from app.schemas import (
    ActualActivityCandidate,
    ActualSetCandidate,
    PlanCandidate,
    PlannedBlockCandidate,
    PlannedSessionCandidate,
    WorkoutCandidate,
)

_DAY_OFFSETS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "понедельник": 0,
    "вторник": 1,
    "среда": 2,
    "четверг": 3,
    "пятница": 4,
    "суббота": 5,
    "воскресенье": 6,
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
    "пн": 0,
    "вт": 1,
    "ср": 2,
    "чт": 3,
    "пт": 4,
    "сб": 5,
    "вс": 6,
}


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _week_day(start: date, label: str) -> date:
    offset = _DAY_OFFSETS.get(label.strip().casefold())
    if offset is None:
        raise ValueError(f"unknown weekday: {label}")
    return start + timedelta(days=(offset - start.weekday()) % 7)


def _is_rest_label(value: str | None) -> bool:
    label = (value or "").casefold()
    return any(word in label for word in ("rest", "recovery day", "отдых", "выходной"))


_DISPLAY_ALIASES = (
    # Curated labels from ready catalog cards.  They are presentation aliases,
    # not a rewrite of the immutable English source snapshot.
    (re.compile(r"\bWarm Up Protocol - Bouldering\b", re.IGNORECASE), "Разминка перед боулдерингом"),
    (re.compile(r"\bDrop Intervals\b", re.IGNORECASE), "Интервалы со снижением сложности"),
    (re.compile(r"\bFive-stage bouldering warm-up\b", re.IGNORECASE), "Пятиэтапная разминка для боулдеринга"),
    (re.compile(r"\bCardiovascular warm-up\b", re.IGNORECASE), "Кардио-разминка"),
    (re.compile(r"\bMobility\b", re.IGNORECASE), "Мобилизация"),
    (re.compile(r"\bEasy climbing\b", re.IGNORECASE), "Лёгкое лазание"),
    (re.compile(r"\bSpecific grip warm-up\b", re.IGNORECASE), "Разминка хвата"),
    (re.compile(r"\bHarder climbing\b", re.IGNORECASE), "Переход к более сложному лазанию"),
    (re.compile(r"\bSix-boulder drop\b", re.IGNORECASE), "Серия из шести боулдеров"),
    (re.compile(r"\bHardest of six nearby boulders\b", re.IGNORECASE), "Самый сложный из шести близких боулдеров"),
    (re.compile(r"\bProgress through five easier boulders with minimal rest\b", re.IGNORECASE), "Пройди ещё пять более лёгких боулдеров с минимальным отдыхом"),
    (re.compile(r"\bChoose a cardio exercise\.?") , "Выбери кардио-упражнение."),
    (re.compile(r"\bArm swings, single-leg RDL, side lunges, IYT and plank twists\.?") , "Махи руками, румынская тяга на одной ноге, боковые выпады, IYT и скручивания в планке."),
    (re.compile(r"\bLow-intensity traversing, easy bouldering or very easy routes\.?") , "Лёгкий траверс, простые боулдеры или очень лёгкие трассы."),
    (re.compile(r"\bUse varied grip positions at low intensity\.?") , "Используй разные хваты с низкой интенсивностью."),
    (re.compile(r"\bUse a few harder moves or problems to prepare for the session\.?") , "Сделай несколько более сложных движений или боулдеров перед основной частью."),
    (re.compile(r"\bRest between sets\.?") , "Отдохни между сериями."),
    (re.compile(r"\bReduce grade if failing or becoming too pumped\.?") , "Снизь сложность, если срываешься или слишком забиваются предплечья."),
    (re.compile(r"\bauto[_ -]?belay\b", re.IGNORECASE), "автостраховка"),
    (re.compile(r"\bавтобейл\b", re.IGNORECASE), "автостраховка"),
    (re.compile(r"\bclimbing[_ -]?gym\b", re.IGNORECASE), "скалодром"),
    (re.compile(r"\bclimbing endurance\b", re.IGNORECASE), "выносливость в скалолазании"),
    (re.compile(r"\bgym\b", re.IGNORECASE), "зал"),
    (re.compile(r"\bhome\b", re.IGNORECASE), "дома"),
    (re.compile(r"\bmonday\b", re.IGNORECASE), "понедельник"),
    (re.compile(r"\btuesday\b", re.IGNORECASE), "вторник"),
    (re.compile(r"\bwednesday\b", re.IGNORECASE), "среда"),
    (re.compile(r"\bthursday\b", re.IGNORECASE), "четверг"),
    (re.compile(r"\bfriday\b", re.IGNORECASE), "пятница"),
    (re.compile(r"\bsaturday\b", re.IGNORECASE), "суббота"),
    (re.compile(r"\bsunday\b", re.IGNORECASE), "воскресенье"),
)
_PARAMETER_LABELS = {
    "duration": "длительность",
    "duration_minutes": "длительность",
    "duration_seconds": "длительность",
    "minutes": "длительность",
    "sets": "подходы",
    "reps": "повторы",
    "sam_volume_sets": "подходы",
    "sam_volume_reps": "повторы",
    "rest_seconds": "отдых",
    "rest_minutes": "отдых",
    "weight": "вес",
    "weight_kg": "вес",
    "load": "нагрузка",
    "distance_m": "дистанция",
    "distance_km": "дистанция",
}
_UNIT_LABELS = {
    "min": "мин", "minute": "мин", "minutes": "мин",
    "sec": "сек", "second": "сек", "seconds": "сек",
    "kg": "кг", "m": "м", "km": "км", "count": "",
}
_WEEKDAYS_RU = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
_MONTHS_RU = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _display_text(value: Any) -> str:
    """Make stored labels readable without changing the frozen source data."""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_display_text(item) for item in value)
    text = str(value)
    for pattern, replacement in _DISPLAY_ALIASES:
        text = pattern.sub(replacement, text)
    return text


def _is_russian_text(value: str) -> bool:
    return bool(re.search(r"[А-Яа-яЁё]", value))


def _compact_seconds(value: Any, *, rest: bool = False) -> str:
    """Keep exact seconds, expressed as minutes when that is clearer."""
    if isinstance(value, (int, float)) and value >= 60 and value % 60 == 0:
        minutes = value / 60
        number = str(int(minutes)) if float(minutes).is_integer() else str(minutes).replace(".", ",")
        return f"{'отдых ' if rest else ''}{number} мин"
    return f"{'отдых ' if rest else ''}{value} сек"


def block_duration_minutes(block: dict[str, Any]) -> float | None:
    """Return one block's duration in minutes only when its unit is explicit.

    This is shared with post-save validation.  It intentionally prefers the
    effective prescription over a catalog snapshot and converts explicit
    seconds rather than ever treating a raw seconds value as minutes.
    """
    effective = block.get("effective_duration") or {}
    value = effective.get("minutes") if isinstance(effective, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    for parameter in block.get("parameters") or []:
        if not isinstance(parameter, dict):
            continue
        name = str(parameter.get("name", "")).casefold()
        unit = str(parameter.get("unit", "")).casefold()
        value = parameter.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if name in {"duration_minutes", "minutes"}:
            return float(value)
        if name in {"duration_seconds", "seconds"}:
            return float(value) / 60
        if name == "duration":
            if unit in {"min", "minute", "minutes", "минута", "минуты", "мин"}:
                return float(value)
            if unit in {"sec", "second", "seconds", "сек", "секунда", "секунды"}:
                return float(value) / 60

    source_duration = (
        ((block.get("protocol_snapshot") or {}).get("specification") or {}).get("duration")
        or {}
    )
    if not isinstance(source_duration, dict):
        return None
    for key in ("estimated_minutes", "minutes", "source_minutes"):
        value = source_duration.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    for key in ("seconds", "duration_seconds"):
        value = source_duration.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value) / 60
    return None


def session_duration_minutes(blocks: list[dict[str, Any]]) -> float | None:
    """Return a total only when every planned block has a known duration."""
    if not blocks:
        return None
    durations = [block_duration_minutes(block) for block in blocks]
    return sum(durations) if all(value is not None for value in durations) else None


def _parameters_text(block: dict[str, Any], *, detailed: bool = True) -> str:
    parameters = block.get("parameters") or []
    rendered = []
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        name = parameter.get("name")
        value = parameter.get("value")
        if not name or value is None:
            continue
        unit = parameter.get("unit")
        unit_text = _UNIT_LABELS.get(str(unit).casefold(), unit) if unit else ""
        suffix = f" {unit_text}" if unit_text else ""
        target = f" ({_display_text(parameter['target'])})" if parameter.get("target") else ""
        label = str(name) if detailed else _PARAMETER_LABELS.get(str(name).casefold(), str(name).replace("_", " "))
        rendered.append(f"{_display_text(label)}: {_display_text(value)}{suffix}{target}")
    return "; ".join(rendered)


def _step_lines(
    steps: list[Any], depth: int = 0, *, detailed: bool = True,
    include_text: bool = True, source_localized_only: bool = False,
) -> list[str]:
    """Render frozen catalog instructions instead of hiding executable steps."""

    lines: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        details: list[str] = []
        visible_text = False
        for key in ("label", "instruction"):
            if not include_text or not step.get(key):
                continue
            candidate = _display_text(step[key])
            # Original instructions remain executable source facts even when
            # not yet curated into Russian; only opaque English labels are
            # replaced by a Russian structural action below.
            if key == "instruction" or not source_localized_only or _is_russian_text(candidate):
                details.append(candidate)
                visible_text = True
        if detailed:
            for key, label in (
                ("sets", "подходов"), ("reps", "повторов"), ("seconds", "сек"),
                ("seconds_min", "сек минимум"), ("seconds_max", "сек максимум"),
                ("rest_seconds", "сек отдыха"), ("rest_seconds_min", "сек отдыха минимум"),
                ("rest_seconds_max", "сек отдыха максимум"),
            ):
                if step.get(key) is not None:
                    details.append(f"{step[key]} {label}")
        else:
            for key, label in (("sets", "подходов"), ("reps", "повторов"), ("seconds", "сек")):
                if step.get(key) is not None:
                    details.append(
                        _compact_seconds(
                            step[key], rest=str(step.get("type", "")).casefold() == "rest"
                        ) if key == "seconds" else f"{step[key]} {label}"
                    )
            for low, high, label in (("seconds_min", "seconds_max", "сек"), ("rest_seconds_min", "rest_seconds_max", "сек отдыха")):
                if step.get(low) is not None or step.get(high) is not None:
                    value = f"{step.get(low, step.get(high))}–{step.get(high, step.get(low))}" if step.get(low) is not None and step.get(high) is not None else str(step.get(low, step.get(high)))
                    details.append(f"{value} {label}")
            if step.get("rest_seconds") is not None:
                details.append(_compact_seconds(step["rest_seconds"], rest=True))
            if not visible_text:
                details.insert(
                    0,
                    {
                        "action": "Упражнение",
                        "rest": "Отдых",
                        "repeat": "Серия",
                        "sequence": "Последовательность",
                        "side": "На сторону",
                    }.get(str(step.get("type", "")).casefold(), "Шаг"),
                )
        if step.get("side") is not None:
            details.append(f"сторона: {_display_text(step['side'])}")
        if step.get("sides") is not None:
            sides = step["sides"]
            sides_text = ", ".join(_display_text(item) for item in sides) if isinstance(sides, list) else _display_text(sides)
            details.append(f"стороны: {sides_text}")
        if details:
            lines.append(f"{'  ' * depth}  – " + "; ".join(details))
        lines.extend(
            _step_lines(
                step.get("children", []), depth + 1,
                detailed=detailed, include_text=include_text,
                source_localized_only=source_localized_only,
            )
        )
    return lines


async def _validate_blocks(
    session: AsyncSession,
    blocks: list[dict[str, Any]],
    athlete_id: int,
    location: str | None = None,
    *,
    source_message_id: int | None = None,
    session_date: date | None = None,
    public: bool = False,
) -> list[dict]:
    """Freeze the catalog snapshot at save time, retaining explicit ad-hoc work."""

    if not blocks:
        return []
    from app.training_profile import effective_training_profile

    source = await session.get(Message, source_message_id) if source_message_id else None
    owner = await session.get(Athlete, athlete_id) if source else None
    timestamp = source.telegram_timestamp if source else None
    if timestamp is not None and timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    profile = await effective_training_profile(
        session, athlete_id=athlete_id, public=public,
        session_text=(source.raw_text or source.transcript) if source else None,
        session_date=session_date,
        reference_date=timestamp.astimezone(ZoneInfo(owner.timezone)).date() if owner and timestamp else None,
    )
    if location:
        profile["session_location"] = location
    # Import lazily: planning remains usable while the catalog inventory is seeded.
    from app.catalog import validate_blocks

    frozen = await validate_blocks(session, blocks, profile=profile)
    # These are plan-specific coach directions, separate from the immutable
    # protocol source snapshot; retain them on the frozen block explicitly.
    for normalized, supplied in zip(frozen, blocks, strict=True):
        for key in ("instruction", "adaptation_reason"):
            if supplied.get(key):
                normalized[key] = supplied[key]
    return frozen


async def _owner_plan(session: AsyncSession, athlete_id: int, plan_id: int) -> Plan:
    plan = await session.get(Plan, plan_id)
    if plan is None or plan.athlete_id != athlete_id:
        raise ValueError("plan not found for athlete")
    return plan


def _content(candidate: PlanCandidate, *, blocks: list[dict], version: int) -> dict[str, Any]:
    # weekly_schedule intentionally stays a map in persisted content for legacy consumers.
    return _clean(
        {
            "version": version,
            "summary": candidate.summary or candidate.title,
            "title": candidate.title,
            "notes": candidate.notes,
            "current_focus": candidate.focus,
            "weekly_schedule": {item.day: item.activity for item in candidate.weekly_schedule},
            "guardrails": candidate.guardrails,
            "goals": [goal.model_dump(mode="json") for goal in candidate.goals],
            "phases": [phase.model_dump(mode="json") for phase in candidate.phases],
            "blocks": blocks,
            "adaptation_reason": candidate.adaptation_reason,
            "location": candidate.location,
        }
    )


async def _replace_children(session: AsyncSession, plan: Plan, today: date) -> None:
    """A replaced week/program invalidates only its own generated child sessions."""

    children = (
        await session.scalars(
            select(Plan).where(
                Plan.athlete_id == plan.athlete_id,
                Plan.parent_id == plan.id,
                Plan.scope == "session",
                Plan.status == "active",
            )
        )
    ).all()
    child_ids = [child.id for child in children]
    recorded_ids = set()
    if child_ids:
        recorded_ids = set(
            (
                await session.scalars(
                    select(Workout.plan_id).where(
                        Workout.athlete_id == plan.athlete_id,
                        Workout.plan_id.in_(child_ids),
                        Workout.status == "active",
                    )
                )
            ).all()
        )
    for child in children:
        if child.start_date >= today and child.id not in recorded_ids:
            child.status = "superseded"


async def _find_relevant_target(
    session: AsyncSession,
    *,
    athlete_id: int,
    candidate: PlanCandidate,
    start: date,
    parent_id: int | None,
) -> Plan | None:
    if candidate.target_plan_id:
        target = await _owner_plan(session, athlete_id, candidate.target_plan_id)
        if target.status != "active":
            raise ValueError("target plan is not active")
        if target.scope != candidate.scope:
            raise ValueError("target plan scope does not match candidate scope")
        return target
    if candidate.action != "update":
        return None
    rows = (
        await session.scalars(
            select(Plan)
            .where(
                Plan.athlete_id == athlete_id,
                Plan.scope == candidate.scope,
                Plan.status == "active",
            )
            .order_by(Plan.created_at.desc())
        )
    ).all()
    # A session/week is identified by its date and direct parent.  Program updates
    # select only a single unambiguous active program rather than retiring all plans.
    matches = [row for row in rows if row.parent_id == parent_id]
    if candidate.scope in {"week", "session"}:
        same_date = [row for row in matches if row.start_date == start]
        # Legacy free-form "update my plan" had no stable target ID.  It is
        # unambiguous only when one scope sibling exists; multiple siblings
        # remain untouched until an explicit target_plan_id is supplied.
        if same_date:
            matches = same_date
    if len(matches) > 1:
        raise ValueError("ambiguous active plan update; provide target_plan_id")
    return matches[0] if matches else None


async def _materialize_sessions(
    session: AsyncSession,
    *,
    parent: Plan,
    candidate: PlanCandidate,
    start: date,
    end: date | None,
    source_message_id: int,
    evidence: list[str],
    visibility: str,
) -> None:
    supplied = list(candidate.sessions)
    explicit_dates = {item.date for item in supplied if item.date is not None}
    # A program's weekly_schedule is a repeating template, not three dated
    # first-week sessions. Only a week plan materializes its schedule; programs
    # materialize explicitly supplied dated sessions.
    schedule_items = candidate.weekly_schedule if candidate.scope == "week" else []
    for item in schedule_items:
        scheduled_day = _week_day(start, item.day)
        # When the AI supplies both a concrete day session and a legacy weekly
        # label, the concrete session is authoritative.  Rest days are not
        # workouts/reminders and therefore never gain a loggable child plan.
        if scheduled_day in explicit_dates or _is_rest_label(item.activity):
            continue
        supplied.append(
            PlannedSessionCandidate(
                date=scheduled_day,
                title=item.activity,
                blocks=[
                    PlannedBlockCandidate(
                        kind="ad_hoc",
                        ad_hoc=True,
                        title=item.activity,
                        source="weekly_schedule",
                    )
                ],
            )
        )
    materialized_days: set[date] = set()
    for supplied_session in supplied:
        session_day = supplied_session.date or (start if len(supplied) == 1 else None)
        if session_day is None:
            raise ValueError("each materialized session needs a date")
        if session_day < start or (end is not None and session_day > end):
            raise ValueError("session date is outside its plan")
        if session_day in materialized_days:
            raise ValueError("duplicate materialized session date")
        materialized_days.add(session_day)
        raw_blocks = [_clean(block.model_dump(mode="json")) for block in supplied_session.blocks]
        blocks = await _validate_blocks(
            session, raw_blocks, parent.athlete_id, supplied_session.location or candidate.location,
            source_message_id=source_message_id, session_date=session_day, public=visibility == "group",
        )
        child_content = _clean(
            {
                "version": 1,
                "summary": supplied_session.title or parent.content.get("summary"),
                "blocks": blocks,
                "notes": supplied_session.notes,
                "adaptation_reason": candidate.adaptation_reason,
                "location": supplied_session.location or candidate.location,
                # Retained for legacy readers which select a materialized child.
                "weekly_schedule": parent.content.get("weekly_schedule", {}),
            }
        )
        child = Plan(
            athlete_id=parent.athlete_id,
            scope="session",
            parent_id=parent.id,
            status="active",
            visibility=visibility,
            start_date=session_day,
            end_date=session_day,
            content=child_content,
            source_message_ids=[source_message_id],
            evidence=evidence,
        )
        session.add(child)


async def save_plan(
    session: AsyncSession,
    *,
    athlete_id: int,
    candidate: PlanCandidate,
    source_message_id: int,
    evidence: list[str],
    today: date,
    visibility: str = "private",
    max_duration_minutes: int | None = None,
) -> Plan:
    """Save one immutable plan revision, and materialize its dated child sessions.

    The transaction/commit belongs to the caller.
    """

    if candidate.action == "none":
        raise ValueError("cannot save a no-op plan")
    if visibility not in {"private", "group"}:
        raise ValueError("invalid plan visibility")
    source = await session.get(Message, source_message_id)
    if source is None or source.athlete_id != athlete_id:
        raise ValueError("plan source is not owned by athlete")

    start = candidate.start_date or today
    end = candidate.end_date
    if candidate.scope == "session":
        if end is not None and end != start:
            raise ValueError("a session plan must start and end on the same day")
        end = end or start
    elif candidate.scope == "week":
        end = end or (start + timedelta(days=6))
    if end is not None and end < start:
        raise ValueError("plan end date precedes start date")

    explicit_target = None
    if candidate.target_plan_id is not None:
        explicit_target = await _owner_plan(session, athlete_id, candidate.target_plan_id)
        if explicit_target.status != "active":
            raise ValueError("target plan is not active")
        if explicit_target.scope != candidate.scope:
            raise ValueError("target plan scope does not match candidate scope")
    parent_id = (
        candidate.parent_plan_id
        if candidate.parent_plan_id is not None
        else (explicit_target.parent_id if explicit_target is not None else None)
    )
    if parent_id is not None:
        parent = await _owner_plan(session, athlete_id, parent_id)
        if parent.status != "active":
            raise ValueError("parent plan is not active")
        allowed = {"program": {"week", "session"}, "week": {"session"}}
        if candidate.scope not in allowed.get(parent.scope, set()):
            raise ValueError("invalid parent scope")
        if start < parent.start_date or (
            parent.end_date is not None and end is not None and end > parent.end_date
        ):
            raise ValueError("child plan dates are outside parent")
        if visibility == "group" and parent.visibility != "group":
            raise ValueError("group plan cannot use a private parent")

    target = await _find_relevant_target(
        session, athlete_id=athlete_id, candidate=candidate, start=start, parent_id=parent_id
    )
    if target is not None and target.parent_id != parent_id:
        raise ValueError("target plan has a different parent")
    if target is not None and visibility == "group" and target.visibility != "group":
        raise ValueError("group plan cannot supersede a private target")
    previous_version = int((target.content or {}).get("version", 0)) if target else 0
    raw_blocks = [_clean(block.model_dump(mode="json")) for block in candidate.blocks]
    blocks = await _validate_blocks(
        session, raw_blocks, athlete_id, candidate.location,
        source_message_id=source_message_id, session_date=start, public=visibility == "group",
    )
    if candidate.scope == "session":
        if max_duration_minutes is None and target:
            max_duration_minutes = (target.content or {}).get("max_duration_minutes")
        if max_duration_minutes is not None:
            total = session_duration_minutes(blocks)
            if total is None or total > max_duration_minutes:
                raise ValueError(
                    f"Full session duration {total} exceeds or cannot verify requested {max_duration_minutes} minutes. "
                    "Include warmup, all catalog source durations, rests and cooldown. Use timed ad_hoc blocks "
                    "if a fixed catalog protocol cannot fit; do not silently shorten its fixed recipe."
                )
    record = Plan(
        athlete_id=athlete_id,
        scope=candidate.scope,
        parent_id=parent_id,
        supersedes_id=target.id if target else None,
        status="active",
        visibility=visibility,
        start_date=start,
        end_date=end,
        content=_content(candidate, blocks=blocks, version=previous_version + 1),
        source_message_ids=[source_message_id],
        evidence=list(evidence),
    )
    if candidate.scope == "session" and max_duration_minutes is not None:
        record.content = {**record.content, "max_duration_minutes": max_duration_minutes}
    if target is not None:
        target.status = "superseded"
        if target.scope in {"program", "week"}:
            await _replace_children(session, target, today)
    session.add(record)
    await session.flush()
    if candidate.scope in {"program", "week"}:
        await _materialize_sessions(
            session,
            parent=record,
            candidate=candidate,
            start=start,
            end=end,
            source_message_id=source_message_id,
            evidence=evidence,
            visibility=visibility,
        )
        await session.flush()
    return record


def render_training_plan(plan: Plan, *, detailed: bool = False) -> str:
    """Render a concise chat plan or a traceable Markdown export.

    The compact view is deliberately only the effective prescription.  The
    detailed export exposes frozen source steps and provenance so an athlete
    can audit an adaptation without mistaking source volume for today's dose.
    """

    content = plan.content or {}
    heading = {"program": "Программа", "week": "Неделя", "session": "Тренировка"}.get(
        plan.scope, "План"
    )
    blocks = [block for block in content.get("blocks") or [] if isinstance(block, dict)]

    if detailed:
        period = plan.start_date.isoformat()
        if plan.end_date and plan.end_date != plan.start_date:
            period += f" – {plan.end_date.isoformat()}"
        lines = [f"# {heading} #{plan.id} · версия {content.get('version', 1)}", f"Период: {period}"]
        if plan.source_message_ids:
            lines.append("Исходные сообщения: " + ", ".join(f"#{item}" for item in plan.source_message_ids))
        if plan.evidence:
            lines.append("Основание: " + "; ".join(_display_text(item) for item in plan.evidence))
        if content.get("summary"):
            lines.append(f"Цель: {_display_text(content['summary'])}")
        if content.get("location"):
            lines.append(f"Место: {_display_text(content['location'])}")
        if content.get("notes"):
            lines.append(f"Заметка: {_display_text(content['notes'])}")
        if content.get("adaptation_reason"):
            lines.append(f"Причина изменения плана: {_display_text(content['adaptation_reason'])}")
        for goal in content.get("goals", []):
            if not isinstance(goal, dict):
                continue
            suffix = f" к {goal['target_date']}" if goal.get("target_date") else ""
            lines.append(f"Цель: {_display_text(goal.get('title', ''))}{suffix}")
        for phase in content.get("phases", []):
            if not isinstance(phase, dict):
                continue
            dates = " – ".join(str(value) for value in (phase.get("start_date"), phase.get("end_date")) if value)
            label = _display_text(phase.get("title", ""))
            if dates:
                label += f" ({dates})"
            if phase.get("objective"):
                label += f": {_display_text(phase['objective'])}"
            lines.append(f"Фаза: {label}")
        for day, activity in (content.get("weekly_schedule") or {}).items():
            lines.append(f"{_display_text(day)}: {_display_text(activity)}")
        for index, block in enumerate(blocks, start=1):
            label = _display_text(block.get("title") or block.get("kind", "block"))
            lines.append(f"## {index}. {label}")
            parameters = _parameters_text(block, detailed=True)
            if parameters:
                lines.append(f"Параметры: {parameters}")
            if block.get("instruction"):
                lines.append(f"Инструкция: {_display_text(block['instruction'])}")
            snapshot = block.get("protocol_snapshot") or {}
            if snapshot:
                provenance = [
                    f"каталог #{block.get('protocol_id')}" if block.get("protocol_id") else "каталог",
                    f"{snapshot.get('slug')} v{snapshot.get('version')}" if snapshot.get("slug") else "",
                ]
                lines.append("Источник: " + ", ".join(item for item in provenance if item))
            elif block.get("ad_hoc") or block.get("source") == "ad_hoc_noncatalog":
                lines.append("Источник: личный блок (не каталог)")
            elif block.get("source"):
                lines.append(f"Источник: {_display_text(block['source'])}")
            if block.get("adaptation_reason"):
                lines.append(f"Причина адаптации: {_display_text(block['adaptation_reason'])}")
            effective_steps = block.get("effective_steps")
            if isinstance(effective_steps, list):
                lines.append("Эффективное назначение Sam:")
                lines.extend(_step_lines(effective_steps, detailed=True))
                duration = block.get("effective_duration") or {}
                if duration.get("kind") == "computed_timer":
                    lines.append(f"Расчётный таймер: {duration.get('minutes')} мин")
                lines.append("Оригинальный рецепт источника:")
            source_steps = (snapshot.get("specification") or {}).get("steps", [])
            if source_steps and not isinstance(effective_steps, list):
                lines.append("Оригинальный рецепт источника:")
            lines.extend(_step_lines(source_steps, detailed=True))
            for restriction in (snapshot.get("specification") or {}).get("restrictions", []):
                if isinstance(restriction, dict) and restriction.get("text"):
                    lines.append(f"Ограничение источника: {_display_text(restriction['text'])}")
        for guardrail in content.get("guardrails") or []:
            lines.append(f"Ограничение плана: {_display_text(guardrail)}")
        return "\n".join(lines)

    def human_day(value: date) -> str:
        return f"{value.day} {_MONTHS_RU[value.month - 1]}, {_WEEKDAYS_RU[value.weekday()]}"

    period = human_day(plan.start_date)
    if plan.end_date and plan.end_date != plan.start_date:
        period = f"{human_day(plan.start_date)} — {human_day(plan.end_date)}"
    lines = [f"{heading} на {period}"]
    # A deliberate summary/title is a better athlete-facing goal than an
    # internal list such as ``['climbing endurance']``.
    goal = content.get("summary") or content.get("title") or content.get("current_focus")
    if not goal:
        first_goal = next((item for item in content.get("goals", []) if isinstance(item, dict)), None)
        goal = first_goal.get("title") if first_goal else None
    if goal:
        lines.append(f"Цель: {_display_text(goal)}")
    block_minutes = [block_duration_minutes(block) for block in blocks]
    minutes = [value for value in block_minutes if value is not None]
    if minutes:
        total = sum(minutes)
        total_text = str(int(total)) if total.is_integer() else str(round(total, 1)).replace(".", ",")
        prefix = "Ориентир по времени" if len(minutes) == len(blocks) else "Известная часть по времени"
        lines.append(f"{prefix}: около {total_text} мин.")
    if content.get("notes"):
        lines.append(f"Примечание: {_display_text(content['notes'])}")
    if plan.scope == "program":
        for phase in content.get("phases") or []:
            if not isinstance(phase, dict):
                continue
            dates = " — ".join(
                str(value) for value in (phase.get("start_date"), phase.get("end_date")) if value
            )
            label = _display_text(phase.get("title") or "этап")
            objective = _display_text(phase.get("objective")) if phase.get("objective") else ""
            suffix = f" ({dates})" if dates else ""
            lines.append(f"• {label}{suffix}" + (f": {objective}" if objective else ""))
    schedule = content.get("weekly_schedule") or {}
    if schedule and plan.scope != "session":
        lines.append("Расписание: " + "; ".join(f"{_display_text(day)} — {_display_text(activity)}" for day, activity in schedule.items()))
    for index, block in enumerate(blocks, start=1):
        label = _display_text(block.get("title") or block.get("kind", "блок"))
        parameters = _parameters_text(block, detailed=False)
        snapshot = block.get("protocol_snapshot") or {}
        is_catalog = bool(snapshot)
        source_suffix = " (каталог)" if is_catalog else (
            " (индивидуальный)"
            if block.get("ad_hoc") or block.get("source") == "ad_hoc_noncatalog"
            else ""
        )
        lines.append(f"{index}. {label}{source_suffix}" + (f" — {parameters}" if parameters else ""))
        if block.get("instruction"):
            instruction = _display_text(block["instruction"])
            if not is_catalog or _is_russian_text(instruction):
                lines.append(f"   {instruction}")
        steps = block.get("effective_steps")
        if not isinstance(steps, list):
            steps = ((snapshot.get("specification") or {}).get("steps", []))
        # Catalog source prose is often English.  The compact Russian view
        # keeps its executable dose/rest and curated Russian action labels,
        # while original wording remains in the detailed export.
        lines.extend(
            _step_lines(
                steps, depth=1, detailed=False, include_text=True,
                source_localized_only=is_catalog,
            )
        )
    safety: list[str] = []
    for block in blocks:
        restrictions = ((block.get("protocol_snapshot") or {}).get("specification") or {}).get("restrictions", [])
        for restriction in restrictions:
            if isinstance(restriction, dict) and restriction.get("text"):
                text = _display_text(restriction["text"])
                if _is_russian_text(text) and text not in safety:
                    safety.append(text)
    for guardrail in content.get("guardrails") or []:
        text = _display_text(guardrail)
        if _is_russian_text(text) and text not in safety:
            safety.append(text)
    if safety:
        lines.append("Важно: " + "; ".join(safety))
    return "\n".join(lines)


async def latest_session_plan(session: AsyncSession, *, athlete_id: int, day: date) -> Plan | None:
    return await session.scalar(
        select(Plan)
        .where(
            Plan.athlete_id == athlete_id,
            Plan.scope == "session",
            Plan.status == "active",
            Plan.start_date == day,
        )
        .order_by(Plan.created_at.desc())
        .limit(1)
    )


async def planning_context(
    session: AsyncSession, *, athlete_id: int, today: date, public: bool = False
) -> dict[str, Any]:
    query = select(Plan).where(Plan.athlete_id == athlete_id, Plan.status == "active")
    if public:
        query = query.where(Plan.visibility == "group")
    rows = (await session.scalars(query.order_by(Plan.created_at.desc()))).all()
    program = next((row for row in rows if row.scope == "program"), None)
    week = next(
        (
            row
            for row in rows
            if row.scope == "week"
            and row.start_date <= today
            and (row.end_date is None or today <= row.end_date)
        ),
        None,
    )
    session_plan = next(
        (row for row in rows if row.scope == "session" and row.start_date == today), None
    )

    def brief(row: Plan | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row.id,
            "scope": row.scope,
            "parent_id": row.parent_id,
            "version": (row.content or {}).get("version", 1),
            "start_date": row.start_date.isoformat(),
            "end_date": row.end_date.isoformat() if row.end_date else None,
            "summary": (row.content or {}).get("summary"),
            "adaptation_reason": (row.content or {}).get("adaptation_reason"),
            "goals": (row.content or {}).get("goals", []),
            "phases": (row.content or {}).get("phases", []),
            "weekly_schedule": (row.content or {}).get("weekly_schedule", {}),
            "guardrails": (row.content or {}).get("guardrails", []),
            "location": (row.content or {}).get("location"),
            "blocks": (row.content or {}).get("blocks", []),
        }

    return {"program": brief(program), "week": brief(week), "session": brief(session_plan)}


async def begin_log(session: AsyncSession, *, athlete_id: int, plan_id: int, now: datetime) -> str:
    """Open a short-lived log context and show exact prescriptions for confirmation."""

    plan = await _owner_plan(session, athlete_id, plan_id)
    if plan.scope != "session" or plan.status != "active":
        raise ValueError("only an active session plan can be logged")
    preference = await session.get(TrainingPreference, athlete_id)
    if preference is None:
        preference = TrainingPreference(athlete_id=athlete_id)
        session.add(preference)
    preference.log_plan_id = plan.id
    existing = await session.scalar(
        select(Workout)
        .where(
            Workout.athlete_id == athlete_id,
            Workout.plan_id == plan.id,
            Workout.status == "active",
        )
        .order_by(Workout.updated_at.desc())
        .limit(1)
    )
    preference.log_workout_id = existing.id if existing else None
    preference.log_expires_at = now + timedelta(hours=2)
    await session.flush()
    lines = [
        f"Записываю тренировку #{plan.id}. Плановые числа ниже — подтвердите или измените; «сделал/всё» не заполнит неизвестные факты:"
    ]
    blocks = (plan.content or {}).get("blocks") or []
    if not blocks:
        lines.append("Числового назначения нет. Напишите, что фактически сделали.")
    for block in blocks:
        label = block.get("title") or block.get("kind", "block")
        parameters = _parameters_text(block)
        lines.append(
            f"• {label}" + (f": {parameters}" if parameters else ": напишите фактические детали")
        )
        snapshot = block.get("protocol_snapshot") or {}
        effective_steps = block.get("effective_steps")
        if isinstance(effective_steps, list):
            lines.append("  Эффективное назначение Sam:")
            lines.extend(_step_lines(effective_steps))
            lines.append("  Оригинальный рецепт источника:")
        lines.extend(_step_lines((snapshot.get("specification") or {}).get("steps", [])))
        for restriction in (snapshot.get("specification") or {}).get("restrictions", []):
            if isinstance(restriction, dict) and restriction.get("text"):
                lines.append(f"  Ограничение: {restriction['text']}")
    return "\n".join(lines)


def _first_parameter(parameters: list[dict[str, Any]], *names: str) -> Any:
    wanted = {name.casefold() for name in names}
    for parameter in parameters:
        if str(parameter.get("name", "")).casefold() in wanted:
            return parameter.get("value")
    return None


def _has_parameter(parameters: list[dict[str, Any]], name: str) -> bool:
    return any(str(parameter.get("name", "")).casefold() == name for parameter in parameters)


def _unit_parameter(parameters: list[dict[str, Any]], name: str, units: set[str]) -> Any:
    for parameter in parameters:
        if str(parameter.get("name", "")).casefold() != name:
            continue
        if str(parameter.get("unit", "")).casefold() not in units:
            return None
        value = parameter.get("value")
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return None


def _bounded_number(value: Any, *, minimum: float, maximum: float) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not minimum <= value <= maximum:
        return None
    return value


def _integral_number(value: Any, *, minimum: int, maximum: int) -> int | None:
    numeric = _bounded_number(value, minimum=minimum, maximum=maximum)
    if numeric is None or not float(numeric).is_integer():
        return None
    return int(numeric)


def _direct_repeat_metrics(block: dict[str, Any]) -> tuple[int, Any, Any] | None:
    """Use only a standalone repeat; composite source recipes require detail."""
    snapshot = block.get("protocol_snapshot") or {}
    steps = block.get("effective_steps")
    if not isinstance(steps, list):
        steps = (snapshot.get("specification") or {}).get("steps", [])
    if not isinstance(steps, list) or len(steps) != 1 or not isinstance(steps[0], dict):
        return None
    repeat = steps[0]
    # A pure sequence wrapper is harmless only when it contains exactly one leaf.
    if repeat.get("type") == "sequence":
        children = repeat.get("children")
        if (
            not isinstance(children, list)
            or len(children) != 1
            or not isinstance(children[0], dict)
        ):
            return None
        if any(key not in {"type", "children", "label", "instruction"} for key in repeat):
            return None
        repeat = children[0]
    if repeat.get("type") != "repeat" or repeat.get("children"):
        return None
    forbidden = {
        "side",
        "sides",
        "left",
        "right",
        "reps_min",
        "reps_max",
        "seconds_min",
        "seconds_max",
        "rest_seconds",
        "rest_seconds_min",
        "rest_seconds_max",
    }
    if forbidden.intersection(repeat):
        return None
    sets = _integral_number(repeat.get("sets"), minimum=1, maximum=100)
    reps = _bounded_number(repeat.get("reps"), minimum=0.00001, maximum=10000)
    seconds = _bounded_number(repeat.get("seconds"), minimum=0.00001, maximum=86400)
    if reps is not None and seconds is not None:
        return None
    return (
        (sets, reps, seconds)
        if sets is not None and (reps is not None or seconds is not None)
        else None
    )


def _confirmed_candidate(
    plan: Plan, displayed_text: str, target_workout_id: int | None
) -> WorkoutCandidate | None:
    """Convert only unambiguous displayed scalar prescriptions after a UI confirm."""

    activities: list[ActualActivityCandidate] = []
    for block in (plan.content or {}).get("blocks", []):
        parameters = block.get("parameters") or []
        category = str(block.get("category", "")).casefold()
        block_kind = str(block.get("kind", "")).casefold()
        title = str(block.get("title") or block.get("kind") or "activity")
        kind = (
            block_kind
            if block_kind in {"strength", "run", "climb", "mobility", "other"}
            else (
                "run"
                if category in {"run", "running"}
                else (
                    "climb"
                    if category in {"climb", "climbing"}
                    else "strength"
                    if category in {"conditioning", "strength"}
                    else "other"
                )
            )
        )
        duration = _bounded_number(
            _unit_parameter(
                parameters,
                "duration_minutes",
                {"min", "minute", "minutes", "мин", "минута", "минут"},
            ),
            minimum=1,
            maximum=1440,
        )
        distance = _bounded_number(
            _unit_parameter(
                parameters,
                "distance_m",
                {"m", "meter", "meters", "metre", "metres", "м", "метр", "метры"},
            ),
            minimum=0.00001,
            maximum=2_000_000,
        )
        sets = _first_parameter(parameters, "sets", "set_count")
        reps = _first_parameter(parameters, "reps", "repetitions")
        weight = _bounded_number(
            _unit_parameter(parameters, "weight_kg", {"kg", "кг"}), minimum=0, maximum=1000
        )
        if (
            (_has_parameter(parameters, "duration_minutes") and duration is None)
            or (_has_parameter(parameters, "distance_m") and distance is None)
            or (_has_parameter(parameters, "weight_kg") and weight is None)
        ):
            return None
        if duration is not None:
            duration = _integral_number(duration, minimum=1, maximum=1440)
        if _has_parameter(parameters, "duration_minutes") and duration is None:
            return None
        if sets is not None:
            set_count = _integral_number(sets, minimum=1, maximum=100)
        else:
            set_count = 0
        if sets is not None and set_count is None:
            return None
        if reps is not None:
            reps = _bounded_number(reps, minimum=0.00001, maximum=10000)
            if reps is None:
                return None
        repeat_metrics = None
        if (
            duration is None
            and distance is None
            and not (set_count and (reps is not None or weight is not None))
        ):
            snapshot = block.get("protocol_snapshot") or {}
            defaults = (snapshot.get("specification") or {}).get("default_parameters", [])
            # A changed catalog parameter must not silently inherit stale source
            # repeat counts that it may have overridden.
            repeat_metrics = (
                _direct_repeat_metrics(block)
                if isinstance(block.get("effective_steps"), list)
                else None
                if snapshot and parameters != defaults
                else _direct_repeat_metrics(block)
            )
            if repeat_metrics is not None:
                set_count, reps, seconds = repeat_metrics
            else:
                seconds = None
        else:
            seconds = None
        actual_sets = [
            ActualSetCandidate(
                set_number=index + 1,
                reps=reps,
                weight_kg=weight,
                duration_seconds=seconds,
                evidence=[displayed_text],
            )
            for index in range(set_count)
            if reps is not None or weight is not None or seconds is not None
        ]
        if duration is None and distance is None and not actual_sets:
            continue
        activities.append(
            ActualActivityCandidate(
                kind=kind,
                title=title,
                duration_minutes=duration,
                distance_m=distance,
                sets=actual_sets,
                evidence=[displayed_text],
            )
        )
    if not activities or len(activities) != len((plan.content or {}).get("blocks", [])):
        return None
    return WorkoutCandidate(
        present=True,
        session_action="append",
        target_workout_id=target_workout_id,
        plan_id=plan.id,
        date=plan.start_date,
        workout_type="training",
        activities=activities,
        confirmed_planned_values=True,
        evidence=[displayed_text],
    )


async def confirm_planned_session(
    session: AsyncSession,
    *,
    athlete_id: int,
    plan_id: int,
    message: Message,
    today: date,
    now: datetime,
) -> tuple[Workout | None, str]:
    """Persist a *button-confirmed displayed* prescription as actual values.

    This is intentionally a separate API: ordinary natural-language ``done``
    cannot reach it and therefore cannot manufacture numbers from a plan.
    """

    plan = await _owner_plan(session, athlete_id, plan_id)
    preference = await session.get(TrainingPreference, athlete_id)
    expires_at = preference.log_expires_at if preference is not None else None
    comparison_now = (
        now.replace(tzinfo=None) if expires_at is not None and expires_at.tzinfo is None else now
    )
    trusted_callback = (
        message.message_type == "callback"
        and (message.processing_data or {}).get("training_action") == "confirm_plan"
        and (message.processing_data or {}).get("plan_id") == plan_id
    )
    if (
        plan.scope != "session"
        or plan.status != "active"
        or message.athlete_id != athlete_id
        or not trusted_callback
        or preference is None
        or preference.log_plan_id != plan_id
        or expires_at is None
        or expires_at < comparison_now
    ):
        return None, "needs_log_confirmation"
    if preference.log_workout_id is not None:
        existing = await session.get(Workout, preference.log_workout_id)
        if (
            existing is not None
            and existing.athlete_id == athlete_id
            and existing.plan_id == plan_id
        ):
            preference.log_plan_id = None
            preference.log_expires_at = None
            return existing, "already_recorded"
    displayed = render_training_plan(plan)
    try:
        candidate = _confirmed_candidate(plan, displayed, preference.log_workout_id)
    except (TypeError, ValueError):
        candidate = None
    if candidate is None:
        return None, "needs_actual_details"
    from app.records import save_session

    workout, status = await save_session(
        session,
        athlete_id=athlete_id,
        message=message,
        candidate=candidate,
        source_text=displayed,
        today=today,
        follow_up_workout_id=preference.log_workout_id,
    )
    if workout is not None:
        preference.log_workout_id = workout.id
        # Consume this authorization.  Telegram's callback dedupe is an extra
        # guard; this prevents a second callback with a different message ID.
        preference.log_plan_id = None
        preference.log_expires_at = None
    return workout, status
