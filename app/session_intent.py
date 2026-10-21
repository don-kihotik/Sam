"""Bounded date and session-follow-up resolution before model planning.

Only a requesting athlete's allowed incoming messages in the current chat are
consulted. The resolver deliberately returns clarification reasons instead of
guessing from unrelated or old training history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Athlete, Message, Plan

_WEEKDAY_PATTERNS = {
    0: r"понедельник(?:а|у|ом|е|и|ов|ам|ами|ах)?|monday(?:s)?",
    1: r"вторник(?:а|у|ом|е|и|ов|ам|ами|ах)?|tuesday(?:s)?",
    2: r"сред(?:а|у|ы|е|ой|ою|ам|ами|ах)?|wednesday(?:s)?",
    3: r"четверг(?:а|у|ом|е|и|ов|ам|ами|ах)?|thursday(?:s)?",
    4: r"пятниц(?:а|ы|е|у|ей|ою|ам|ами|ах)?|friday(?:s)?",
    5: r"суббот(?:а|ы|е|у|ой|ою|ам|ами|ах)?|saturday(?:s)?",
    6: r"воскресень(?:е|я|ю|ем|и|ям|ями|ях)?|sunday(?:s)?",
}
_FOLLOW_UP = re.compile(
    r"какие\s+упражнен\w*|можно\s+короче|сделай\s+короче|"
    r"сократ\w*|замен\w*|перенес\w*|могу\s+комбиниров\w*|"
    r"могу\s+(?:и|также)\b|что\s+посовет\w*|посовет\w*|что\s+делать|"
    r"можно\s+комбиниров\w*|what\s+exercises|which\s+exercises|"
    r"make\s+it\s+shorter|can\s+i\s+combine|reschedul\w*|replace\w*",
    re.IGNORECASE,
)
_REFINEMENT = re.compile(
    r"сократ\w*|замен\w*|перенес\w*|короче|shorter|reschedul\w*|replace\w*",
    re.IGNORECASE,
)
# A natural report such as "тренировка 30 минут" must never turn 30 into a plan ID.
_PLAN_ID = re.compile(
    r"/(?:session|log)\s+#?(\d+)\b|"
    r"(?:план|сесси\w*|тренировк\w*|session|workout)\s*(?:id|#|№)\s*(\d+)\b",
    re.IGNORECASE,
)
_PERFORMED_REPORT = re.compile(
    r"\b(?:сделал(?:а|и)?|выполнил(?:а|и)?|пробежал(?:а|и)?|"
    r"потренировал(?:ся|ась|ись)?|лазал(?:а|и)?|ran|completed|did)\b",
    re.IGNORECASE,
)
_RECENT_LIMIT = 20


@dataclass(frozen=True)
class SessionIntentResolution:
    date: date | None = None
    target_plan_id: int | None = None
    ambiguity: str | None = None


def _explicit_dates(text: str, today: date) -> tuple[set[date], str | None]:
    """Extract exact date intent; ambiguous relative language is never guessed."""

    values: set[date] = set()
    iso_values: set[date] = set()
    for raw in re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", text):
        try:
            iso_values.add(date.fromisoformat(raw))
        except ValueError:
            return set(), "invalid_iso_date"
    values.update(iso_values)
    if re.search(r"\b(?:сегодня|today)\b", text, re.IGNORECASE):
        values.add(today)
    if re.search(r"\b(?:завтра|tomorrow)\b", text, re.IGNORECASE):
        values.add(today + timedelta(days=1))
    if re.search(
        r"\b(?:прошл\w*|last)\s+(?:week|недел\w*|"
        r"понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*|"
        r"суббот\w*|воскресень\w*|monday(?:s)?|tuesday(?:s)?|"
        r"wednesday(?:s)?|thursday(?:s)?|friday(?:s)?|saturday(?:s)?|sunday(?:s)?)\b",
        text,
        re.IGNORECASE,
    ):
        return set(), "ambiguous_past_weekday"
    if re.search(r"\b(?:выходн\w*|weekend)\b", text, re.IGNORECASE):
        return set(), "ambiguous_weekend"
    if re.search(r"\b(?:следующ\w*|next)\s+(?:week|недел\w*)\b", text, re.IGNORECASE):
        return set(), "ambiguous_next_week"
    for weekday, pattern in _WEEKDAY_PATTERNS.items():
        match = re.search(rf"\b(?:{pattern})\b", text, re.IGNORECASE)
        if not match:
            continue
        if iso_values:
            if any(value.weekday() != weekday for value in iso_values):
                return set(), "conflicting_dates"
            continue
        delta = (weekday - today.weekday()) % 7
        qualifier = re.search(
            rf"\b(?:следующ\w*|next)\s+(?:{pattern})\b", text, re.IGNORECASE
        )
        values.add(today + timedelta(days=delta + (7 if qualifier else 0)))
    if len(values) > 1:
        return set(), "conflicting_dates"
    return values, None


def _current_policy_time(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    return current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)


def _message_text(message: Message) -> str:
    return message.raw_text or message.transcript or message.normalized_text or ""


def _allowed_request(message: Message) -> bool:
    decision = (message.processing_data or {}).get("request_policy", {}).get("decision")
    return decision in {None, "allow"}


def _timestamp_key(message: Message) -> tuple[datetime, int]:
    timestamp = message.telegram_timestamp
    normalized = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp.astimezone(UTC)
    return normalized, message.id


def _plan_ids(text: str) -> set[int]:
    return {int(value) for groups in _PLAN_ID.findall(text) for value in groups if value}


def _message_local_day(message: Message, *, fallback: date, timezone: str) -> date:
    timestamp = _timestamp_key(message)[0]
    try:
        return timestamp.astimezone(ZoneInfo(timezone)).date()
    except ZoneInfoNotFoundError:
        return fallback


def _recent_date_request(message: Message, *, fallback_today: date, timezone: str) -> date | None:
    """Return only a planning date, never a performed-session report date."""

    text = _message_text(message)
    values, ambiguity = _explicit_dates(
        text,
        _message_local_day(message, fallback=fallback_today, timezone=timezone),
    )
    if ambiguity or not values or _PERFORMED_REPORT.search(text):
        return None
    return next(iter(values))


async def _owned_session_plan(
    session: AsyncSession, athlete_id: int, plan_id: int, *, group_chat: bool
) -> Plan | None:
    plan = await session.get(Plan, plan_id)
    if not plan or plan.athlete_id != athlete_id or plan.scope != "session" or plan.status != "active":
        return None
    if group_chat and plan.visibility != "group":
        return None
    return plan


async def _recent_messages(
    session: AsyncSession,
    *,
    athlete_id: int,
    chat_id: int,
    cutoff: datetime,
    now: datetime,
    current_message_id: int | None,
) -> list[Message]:
    query = select(Message).where(
        Message.athlete_id == athlete_id,
        Message.telegram_chat_id == chat_id,
        Message.direction == "incoming",
        Message.telegram_timestamp >= cutoff,
        Message.telegram_timestamp <= now,
        or_(
            Message.processing_data["request_policy"]["decision"].as_string().is_(None),
            Message.processing_data["request_policy"]["decision"].as_string() == "allow",
        ),
    )
    if current_message_id is not None:
        query = query.where(Message.id != current_message_id)
    rows = (
        await session.scalars(query.order_by(desc(Message.telegram_timestamp), desc(Message.id)).limit(_RECENT_LIMIT))
    ).all()
    return [message for message in rows if _allowed_request(message) and _timestamp_key(message)[0] <= now]


async def _discussed_plan(
    session: AsyncSession,
    *,
    athlete_id: int,
    source_messages: list[Message],
    group_chat: bool,
    target_date: date | None = None,
) -> tuple[Plan | None, tuple[datetime, int] | None, str | None]:
    """Find exactly one most-recent active session referenced by this conversation."""

    source_by_id = {message.id: message for message in source_messages}
    if not source_by_id:
        return None, None, None
    query = select(Plan).where(
        Plan.athlete_id == athlete_id,
        Plan.scope == "session",
        Plan.status == "active",
    )
    if group_chat:
        query = query.where(Plan.visibility == "group")
    plans = (await session.scalars(query)).all()
    candidates: list[tuple[Plan, tuple[datetime, int]]] = []
    for plan in plans:
        if target_date is not None and plan.start_date != target_date:
            continue
        linked = [source_by_id[source_id] for source_id in (plan.source_message_ids or []) if source_id in source_by_id]
        if linked:
            candidates.append((plan, max(_timestamp_key(message) for message in linked)))
    if not candidates:
        return None, None, None
    latest_key = max(key for _, key in candidates)
    latest = [plan for plan, key in candidates if key == latest_key]
    if len(latest) != 1:
        return None, latest_key, "ambiguous_discussed_session"
    return latest[0], latest_key, None


async def resolve_session_intent(
    session: AsyncSession,
    *,
    athlete_id: int,
    chat_id: int,
    text: str,
    today: date,
    now: datetime | None = None,
    current_message_id: int | None = None,
) -> SessionIntentResolution:
    """Resolve an owned current-conversation plan/day without global similarity matching."""

    values, ambiguity = _explicit_dates(text, today)
    if ambiguity:
        return SessionIntentResolution(ambiguity=ambiguity)
    plan_ids = _plan_ids(text)
    if len(plan_ids) > 1:
        return SessionIntentResolution(ambiguity="conflicting_plan_ids")
    group_chat = chat_id < 0
    if plan_ids:
        plan = await _owned_session_plan(session, athlete_id, plan_ids.pop(), group_chat=group_chat)
        if plan is None:
            return SessionIntentResolution(ambiguity="unknown_or_inactive_session")
        if values and plan.start_date not in values and not _REFINEMENT.search(text):
            return SessionIntentResolution(ambiguity="conflicting_plan_date")
        return SessionIntentResolution(
            date=next(iter(values)) if values else plan.start_date,
            target_plan_id=plan.id,
        )

    if not _FOLLOW_UP.search(text):
        return SessionIntentResolution(date=next(iter(values)) if values else None)

    policy_now = _current_policy_time(now)
    athlete = await session.get(Athlete, athlete_id)
    athlete_timezone = athlete.timezone if athlete else "UTC"
    messages = await _recent_messages(
        session,
        athlete_id=athlete_id,
        chat_id=chat_id,
        cutoff=policy_now - timedelta(hours=24),
        now=policy_now,
        current_message_id=current_message_id,
    )
    requested_date = next(iter(values)) if values else None
    plan, plan_key, plan_ambiguity = await _discussed_plan(
        session,
        athlete_id=athlete_id,
        source_messages=messages,
        group_chat=group_chat,
        # A same-day follow-up can amend only that discussed session. A
        # reschedule intentionally targets the prior session on another day.
        target_date=requested_date if requested_date is not None and not _REFINEMENT.search(text) else None,
    )
    if plan_ambiguity:
        return SessionIntentResolution(ambiguity=plan_ambiguity)

    latest_date: date | None = None
    latest_date_key: tuple[datetime, int] | None = None
    for message in messages:
        candidate = _recent_date_request(
            message,
            fallback_today=today,
            timezone=athlete_timezone,
        )
        if candidate is not None:
            latest_date, latest_date_key = candidate, _timestamp_key(message)
            break

    if requested_date is not None:
        if plan and (_REFINEMENT.search(text) or plan.start_date == requested_date):
            return SessionIntentResolution(date=requested_date, target_plan_id=plan.id)
        return SessionIntentResolution(date=requested_date)
    if latest_date is not None:
        if plan_key is None or latest_date_key is not None and latest_date_key > plan_key:
            return SessionIntentResolution(date=latest_date)
        if latest_date_key == plan_key and plan is not None:
            return SessionIntentResolution(date=latest_date, target_plan_id=plan.id)
    if plan is not None:
        return SessionIntentResolution(date=plan.start_date, target_plan_id=plan.id)
    return SessionIntentResolution(date=latest_date)
