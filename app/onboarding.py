"""Deterministic onboarding state and first-session handoff.

The model may extract evidence-backed profile fields, but it must not decide
that answering the profile questionnaire is permission to create a workout.
This module owns that small product state machine.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from app.db.models import TrainingPreference

_KEY = "_onboarding"
_READINESS = re.compile(
    r"самочувств|чувствую|боли?\s+(?:нет|есть)|болит|без\s+боли|"
    r"устал|свеж|здоров|нормальн|хорошо|плохо|"
    r"feel|pain|sore|fatigue|fresh|healthy",
    re.IGNORECASE,
)
_MINUTES = re.compile(r"\b(\d{1,3})\s*(?:мин\b|минут\w*|minutes?\b)", re.IGNORECASE)


@dataclass(slots=True)
class OnboardingReply:
    text: str
    payload: dict = field(default_factory=dict)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _profile(preference: TrainingPreference) -> dict:
    return copy.deepcopy(preference.profile or {})


def start_profile_onboarding(preference: TrainingPreference, *, now: datetime) -> None:
    profile = _profile(preference)
    current = profile.get(_KEY)
    if isinstance(current, dict) and current.get("status") not in {"cancelled", "completed"}:
        return
    profile[_KEY] = {
        "status": "collecting_profile",
        "started_at": _aware(now).isoformat(),
    }
    preference.profile = profile


def profile_onboarding_reply(
    preference: TrainingPreference, *, now: datetime
) -> OnboardingReply | None:
    """Return the next questionnaire step, or finish with an explicit plan CTA."""
    profile = _profile(preference)
    state = profile.get(_KEY)
    if not isinstance(state, dict) or state.get("status") != "collecting_profile":
        return None

    prompts = (
        ("goals", "Какая у тебя основная цель в лазании на ближайшие месяцы?"),
        (
            "experience",
            "Какой у тебя текущий уровень и формат лазания: боулдеринг, трассы или оба?",
        ),
        (
            "availability",
            "Сколько раз в неделю реально можешь тренироваться и есть ли ограничение по времени?",
        ),
        (
            "locations",
            "Где обычно тренируешься и что там доступно: боулдеринг, трассы, турник, кампус, доска или другое?",
        ),
    )
    for field_name, question in prompts:
        values = profile.get(field_name)
        if not isinstance(values, list) or not values:
            state["last_prompt"] = field_name
            state["updated_at"] = _aware(now).isoformat()
            profile[_KEY] = state
            preference.profile = profile
            return OnboardingReply(
                "Хорошо, это запомнил. " + question + " После анкеты предложу первый полезный шаг."
            )

    state.update(
        {
            "status": "profile_ready",
            "completed_at": _aware(now).isoformat(),
            "last_prompt": None,
        }
    )
    profile[_KEY] = state
    preference.profile = profile
    return OnboardingReply(
        "Профиль готов. Следующий полезный шаг — собрать первое занятие именно под тебя. "
        "Нажми кнопку ниже: я уточню день, доступное время и текущее самочувствие, "
        "а затем предложу конкретную тренировку из подходящих упражнений.",
        {"buttons": [[{"text": "Составить первую тренировку", "data": "onboard:first"}]]},
    )


def start_first_session(preference: TrainingPreference, *, now: datetime) -> None:
    profile = _profile(preference)
    state = profile.get(_KEY) if isinstance(profile.get(_KEY), dict) else {}
    state.update(
        {
            "status": "awaiting_first_session_details",
            "first_session_requested_at": _aware(now).isoformat(),
        }
    )
    profile[_KEY] = state
    preference.profile = profile


def first_session_pending(preference: TrainingPreference) -> bool:
    state = (preference.profile or {}).get(_KEY)
    return isinstance(state, dict) and state.get("status") == "awaiting_first_session_details"


def first_session_minutes(text: str) -> int | None:
    match = _MINUTES.search(text)
    if match is None:
        return None
    value = int(match.group(1))
    return value if 10 <= value <= 600 else None


def first_session_missing(
    text: str, *, session_date: date | None
) -> tuple[str, ...]:
    missing: list[str] = []
    if session_date is None:
        missing.append("день тренировки")
    if first_session_minutes(text) is None:
        missing.append("сколько минут есть на занятие")
    if _READINESS.search(text) is None:
        missing.append("самочувствие и есть ли сейчас боль")
    return tuple(missing)


def first_session_clarification(missing: tuple[str, ...]) -> OnboardingReply:
    fields = ", ".join(missing)
    return OnboardingReply(
        f"Отлично, сделаем первую тренировку. Осталось уточнить: {fields}. "
        "Ответь одним сообщением — после этого сразу соберу занятие. "
        "Например: «в субботу, 90 минут, чувствую себя хорошо, боли нет»."
    )


def first_session_context(*, session_date: date, minutes: int) -> str:
    return (
        "\nFIRST SESSION ONBOARDING (verified application state):\n"
        f"The athlete explicitly requested a first session, supplied date={session_date.isoformat()} "
        f"and total_time_minutes={minutes}, and answered the current-readiness question. "
        "Create one concrete session plan now unless the reported condition makes training unsafe. "
        "Do not exceed total_time_minutes."
    )


def complete_first_session(preference: TrainingPreference, *, plan_id: int, now: datetime) -> None:
    profile = _profile(preference)
    state = profile.get(_KEY)
    if not isinstance(state, dict):
        return
    state.update(
        {
            "status": "completed",
            "first_plan_id": plan_id,
            "first_plan_created_at": _aware(now).isoformat(),
        }
    )
    profile[_KEY] = state
    preference.profile = profile

