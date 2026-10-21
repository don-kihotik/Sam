"""Evidence-backed training preferences and catalog-eligibility profiles."""

from __future__ import annotations

import copy
import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Athlete, Fact, Message, TrainingPreference
from app.membership import ensure_preference
from app.records import evidence_in, stated_number
from app.session_intent import _explicit_dates

_FLASH_MARKER = re.compile(r"фл[эе]ш|flash", re.IGNORECASE)
_ONSIGHT_MARKER = re.compile(r"онсайт|он[- ]?сайт|onsight|on[- ]?sight", re.IGNORECASE)


def _normalize_experience_grades(value: dict, evidence: list[str]) -> dict:
    """Do not turn a general climbing level into a flash/onsight claim."""
    result = dict(value)
    source = " ".join(evidence)
    for field, marker in (
        ("flash_grade", _FLASH_MARKER),
        ("onsight_grade", _ONSIGHT_MARKER),
    ):
        grade = result.get(field)
        if grade and marker.search(source) is None:
            result.setdefault("level", grade)
            result.pop(field, None)
    return result


def repair_unqualified_experience_grades(profile: dict) -> dict:
    """Repair older profile rows when their own evidence lacks grade semantics."""
    updated = copy.deepcopy(profile or {})
    experiences = updated.get("experience")
    if not isinstance(experiences, list):
        return updated
    repaired = []
    for item in experiences:
        if not isinstance(item, dict):
            repaired.append(item)
            continue
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            repaired.append(item)
            continue
        repaired.append(
            _normalize_experience_grades(item, [str(quote) for quote in evidence])
        )
    updated["experience"] = repaired
    return updated


async def apply_training_profile(
    session: AsyncSession, *, athlete: Athlete, message: Message, candidate, text: str
) -> list[dict]:
    preference = await ensure_preference(session, athlete)
    profile = dict(preference.profile or {})
    changes = []
    keys = {
        "goals": "title",
        "availability": "day",
        "locations": "name",
        "equipment": "name",
        "experience": "activity",
        "partners": "name",
    }
    replacements = (
        set(getattr(candidate, "replace_fields", []))
        if evidence_in(text, getattr(candidate, "evidence", []))
        else set()
    )
    for field, identity in keys.items():
        accepted = []
        for item in getattr(candidate, field):
            quotes = evidence_in(text, item.evidence)
            if not quotes:
                continue
            value = item.model_dump(mode="json", exclude_none=True)
            if field == "experience":
                value = _normalize_experience_grades(value, quotes)
            # Numerical availability cannot be inferred from 'evening'.
            if "minutes" in value and not stated_number(" ".join(quotes), value["minutes"]):
                value.pop("minutes")
            value["evidence"] = quotes
            value["source_message_id"] = message.id
            value["visibility"] = "group" if message.telegram_chat_id < 0 else "private"
            accepted.append(value)
        if not accepted:
            continue
        existing = [] if field in replacements else list(profile.get(field, []))
        for value in accepted:
            key = (str(value.get(identity, "")).casefold(), value.get("location"))
            existing = [
                old
                for old in existing
                if (str(old.get(identity, "")).casefold(), old.get("location")) != key
            ]
            existing.append(value)
        profile[field] = existing
        changes.append({"kind": "training_profile", "field": field, "count": len(accepted)})
    if changes:
        preference.profile = profile
    return changes


def visible_profile(profile: dict, *, public: bool) -> dict:
    """Never expose source text or private location/availability in a group prompt."""
    result = {}
    for field in ("goals", "availability", "locations", "equipment", "experience", "partners"):
        values = profile.get(field, [])
        if not isinstance(values, list):
            continue
        result[field] = [
            {
                k: v
                for k, v in item.items()
                if k not in {"evidence", "source_message_id", "visibility"}
            }
            for item in values
            if isinstance(item, dict) and (not public or item.get("visibility") == "group")
        ]
    return result


def _availability_dates(text: str, reference_date: date) -> tuple[set[date], str | None]:
    """Use the same guarded date grammar as session-intent resolution."""
    return _explicit_dates(text, reference_date)


def _matches_session_date(text: str, *, reference_date: date, session_date: date | None) -> bool:
    """Reject ambiguous/conflicting date references instead of widening access."""
    dates, ambiguity = _availability_dates(text, reference_date)
    return ambiguity is None and len(dates) == 1 and session_date == next(iter(dates))


def _gym_equipment_from_text(text: str) -> list[str]:
    """Map only explicit gym-access words to the catalog's exact equipment IDs."""
    folded = text.casefold()
    positive_context = r"(?:могу|доступ(?:ен|на)?|есть|буду|планирую|только)"
    def mentioned(terms: str) -> bool:
        if re.search(rf"(?:нет|без|вместо)\s+(?:\w+\s+){{0,2}}{terms}", folded):
            return False
        return bool(
            re.search(rf"{positive_context}.{{0,32}}{terms}|{terms}.{{0,32}}{positive_context}", folded)
            or re.search(r"\b(?:bouldering|болдеринг)\s+(?:and|и|&|с)\s+(?:autobelays?|автострахов\w*)", folded)
        )
    equipment = []
    if mentioned(r"(?:болдер\w*|боулдер\w*|boulder\w*)"):
        equipment.append("bouldering wall")
    if mentioned(r"(?:авто\s*-?страх\w*|auto\s*-?belay\w*|autobelay\w*)"):
        # Auto-belay access establishes access to its wall, but never a lead
        # qualification, partner, grade, or any other climbing ability.
        equipment.extend(["route or circuit wall", "auto_belay"])
    return list(dict.fromkeys(equipment))


def _unavailable_gym_equipment_from_text(text: str) -> set[str]:
    """Session-local denials override inherited availability without persistence."""
    folded = text.casefold()
    unavailable = set()
    if re.search(r"(?:нет|без|вместо)\s+(?:\w+\s+){0,2}(?:болдер\w*|боулдер\w*|boulder\w*)", folded):
        unavailable.add("bouldering wall")
    if re.search(r"(?:нет|без|вместо)\s+(?:\w+\s+){0,2}(?:авто\s*-?страх\w*|auto\s*-?belay\w*|autobelay\w*)", folded):
        unavailable.add("auto_belay")
    # "Only bouldering" is an explicit current-session restriction, so route
    # wall and auto-belay facts are not silently combined with it.
    if re.search(r"только\s+(?:болдер\w*|боулдер\w*|boulder\w*)", folded):
        unavailable.update({"route or circuit wall", "auto_belay"})
    return unavailable


def _has_explicit_unavailable(profile: dict, name: str) -> bool:
    return any(
        isinstance(item, dict)
        and str(item.get("name", "")).casefold() == name.casefold()
        and item.get("available") is False
        for item in profile.get("equipment", [])
    )


def _add_gym_equipment(profile: dict, equipment: list[str]) -> None:
    if not equipment:
        return
    values = list(profile.get("equipment", []))
    existing = {
        str(item.get("name", "")).casefold()
        for item in values
        if isinstance(item, dict) and item.get("available") is True
    }
    for name in equipment:
        if name.casefold() in existing or _has_explicit_unavailable(profile, name):
            continue
        values.append({"name": name, "available": True, "location": "climbing_gym"})
        existing.add(name.casefold())
    profile["equipment"] = values
    usable = any(
        isinstance(item, dict)
        and str(item.get("name", "")).casefold() in {name.casefold() for name in equipment}
        and item.get("available") is True
        for item in values
    )
    if not usable:
        return
    locations = list(profile.get("locations", []))
    if not any(
        (item.get("name") if isinstance(item, dict) else item) == "climbing_gym"
        for item in locations
    ):
        locations.append({"name": "climbing_gym"})
    profile["locations"] = locations


def _apply_session_unavailability(profile: dict, unavailable: set[str]) -> None:
    if not unavailable:
        return
    profile["equipment"] = [
        (
            {**item, "available": False}
            if isinstance(item, dict)
            and str(item.get("name", "")).casefold()
            in {name.casefold() for name in unavailable}
            and item.get("available") is True
            else item
        )
        for item in profile.get("equipment", [])
    ]


async def effective_training_profile(
    session: AsyncSession,
    *,
    athlete_id: int,
    public: bool = False,
    session_text: str | None = None,
    session_date: date | None = None,
    reference_date: date | None = None,
) -> dict:
    """Build a catalog profile from visible explicit data and current access evidence.

    Stored weekday-specific gym access is used only when its source names the
    requested session day.  ``session_text`` is intentionally transient: it can
    make the current request eligible but is never written to preferences.
    """
    athlete = await session.get(Athlete, athlete_id)
    preference = await session.get(TrainingPreference, athlete_id)
    profile = copy.deepcopy(
        visible_profile(dict(preference.profile or {}) if preference else {}, public=public)
    )
    facts_query = (
        select(Fact, Message)
        .join(Message, Fact.source_message_id == Message.id)
        .where(
            Fact.athlete_id == athlete_id,
            Fact.field == "gym_access",
            Fact.status == "active",
            Message.athlete_id == athlete_id,
        )
        .order_by(desc(Fact.id))
    )
    if public:
        facts_query = facts_query.where(Fact.visibility == "group", Message.telegram_chat_id < 0)
    gym_equipment: list[str] = []
    # A later active record is the only current gym-access assertion.  Do not
    # union an older positive fact with a newer contradictory one.
    row = (await session.execute(facts_query.limit(1))).first()
    if row:
        fact, source = row
        source_text = " ".join(part for part in (source.raw_text, source.normalized_text) if part)
        timezone = ZoneInfo(athlete.timezone) if athlete else ZoneInfo("UTC")
        timestamp = source.telegram_timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        source_date = timestamp.astimezone(timezone).date()
        dated, ambiguity = _availability_dates(source_text, source_date)
        if ambiguity is None and (not dated or _matches_session_date(
            source_text, reference_date=source_date, session_date=session_date
        )):
            # Fact value is the configuration.  Source text controls only its
            # timing/provenance and is never mined for another person's access.
            gym_equipment.extend(_gym_equipment_from_text(str(fact.value or "")))
    # The current scoped request may state a one-off venue.  It is evidence for
    # this selection only and is deliberately not persisted as a preference.
    current_date = reference_date or datetime.now(
        ZoneInfo(athlete.timezone) if athlete else ZoneInfo("UTC")
    ).date()
    if session_text:
        dates, ambiguity = _availability_dates(session_text, current_date)
    else:
        dates, ambiguity = set(), None
    session_applies = bool(session_text and ambiguity is None and (
        not dates or _matches_session_date(
            session_text, reference_date=current_date, session_date=session_date
        )
    ))
    unavailable = _unavailable_gym_equipment_from_text(session_text or "") if session_applies else set()
    if session_applies:
        gym_equipment.extend(_gym_equipment_from_text(session_text))
    _apply_session_unavailability(profile, unavailable)
    _add_gym_equipment(
        profile,
        [name for name in dict.fromkeys(gym_equipment) if name not in unavailable],
    )
    return profile
