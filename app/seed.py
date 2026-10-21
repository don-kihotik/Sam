"""Idempotent setup and explicitly approved September product preferences."""

from datetime import UTC, date, datetime
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    Athlete,
    Correction,
    Event,
    Fact,
    Message,
    Plan,
    TrainingPreference,
    Workout,
    WorkoutEntry,
)
from app.grades import grade_rank
from app.membership import ensure_shared_chat, grant_group_access

ALEXEY_PROFILE = {
    "identity": {"name": "Alexey"},
    "physical": {"height_cm": 193, "starting_weight_kg": 104},
    "climbing_background": {
        "previously_climbed_regularly": True,
        "break_years": "several",
        "resumed": "2026-08",
    },
    "current_level": {
        "bouldering": {
            "V1": "successful",
            "V2": "successful",
            "V3": "some successful, some projects",
            "V4": "attempted",
        },
        "routes": "approximately YDS 5.8–5.9 on auto belay",
    },
    "availability": {
        "preferred_climbing_days": ["Tuesday", "Thursday", "weekend"],
    },
    "goals": ["Prepare physically for a guided multipitch trip in late November 2026"],
    "learned_patterns": [
        {
            "statement": "Inside-corner routes may currently be more energy-efficient",
            "status": "hypothesis",
            "confidence": 0.35,
        }
    ],
    "baseline": {
        "bouldering": ["2×V1", "3×V2", "2×V3", "1×V3 incomplete", "V4 attempted"],
        "auto_belay": ["3×5.9", "2×5.8", "3×5.9"],
        "end_state": {
            "forearm_pump": "6–7/10",
            "reserve": "meaningful",
            "pain": "none reported in fingers, elbows or shoulders",
        },
    },
}


async def seed_initial_data(session: AsyncSession, settings: Settings) -> None:
    athletes = {}
    for name, uid, profile in (
        ("Alexey", settings.alexey_telegram_user_id, ALEXEY_PROFILE),
        ("Andrey", settings.andrey_telegram_user_id, {"identity": {"name": "Andrey"}}),
    ):
        athlete = await session.scalar(select(Athlete).where(Athlete.name == name))
        if athlete is None:
            athlete = Athlete(
                name=name, telegram_user_id=uid, profile=profile, timezone=settings.timezone
            )
            session.add(athlete)
            await session.flush()
        elif uid is not None:
            athlete.telegram_user_id = uid
        athletes[name] = athlete
        # One-time legacy consent. Never overwrite /sharing off or notification preferences.
        preference = await session.get(TrainingPreference, athlete.id)
        if preference is None:
            previous_private = await session.scalar(
                select(Message.telegram_chat_id)
                .where(Message.athlete_id == athlete.id, Message.telegram_chat_id > 0)
                .order_by(Message.id.desc())
                .limit(1)
            )
            session.add(
                TrainingPreference(
                    athlete_id=athlete.id,
                    shared_chat_id=settings.telegram_allowed_chat_id,
                    private_chat_id=previous_private,
                    profile={},
                )
            )
    alexey = athletes["Alexey"]
    # Backward-compatible bridge from the former single shared_chat_id field
    # into the multi-group membership tables. A NULL value remains an opt-out.
    legacy_preferences = (
        await session.scalars(
            select(TrainingPreference).where(TrainingPreference.shared_chat_id.is_not(None))
        )
    ).all()
    for preference in legacy_preferences:
        member = await session.get(Athlete, preference.athlete_id)
        if member is None or not member.active:
            continue
        await ensure_shared_chat(
            session,
            telegram_chat_id=preference.shared_chat_id,
            title=None,
            created_by=alexey,
        )
        await grant_group_access(
            session,
            athlete=member,
            telegram_chat_id=preference.shared_chat_id,
            role="admin" if member.id == alexey.id else "member",
        )
    baseline = await session.scalar(
        select(Workout)
        .where(
            Workout.athlete_id == alexey.id,
            Workout.date.is_(None),
            Workout.notes.like("Baseline session reported%"),
        )
        .limit(1)
    )
    if baseline is None:
        session.add(
            Workout(
                athlete_id=alexey.id,
                date=None,
                date_precision="month",
                type="climbing",
                notes="Baseline session reported during initial product setup; exact date unknown.",
                structured_details={
                    "pump": None,
                    "pump_range": [6, 7],
                    "date_label": "August 2026",
                },
                pain_status="reported_none",
                source_message_ids=[],
                evidence=["baseline imported from the agreed athlete profile"],
                fingerprint=sha256(b"alexey-baseline-2026-08").hexdigest(),
                entries=[
                    WorkoutEntry(
                        discipline=discipline,
                        grade_system=system,
                        original_grade=grade,
                        grade_rank=grade_rank(system, grade),
                        count=count,
                        completed_count=complete,
                    )
                    for discipline, system, grade, count, complete in [
                        ("bouldering", "V", "V1", 2, 2),
                        ("bouldering", "V", "V2", 3, 3),
                        ("bouldering", "V", "V3", 3, 2),
                        ("bouldering", "V", "V4", 1, 0),
                        ("auto_belay", "YDS", "5.8", 2, 2),
                        ("auto_belay", "YDS", "5.9", 6, 6),
                    ]
                ],
            )
        )
    # A single provenance record makes this revision one-time, not a startup overwrite.
    chat_id = settings.telegram_allowed_chat_id or -1
    source = await session.scalar(
        select(Message).where(
            Message.telegram_chat_id == chat_id,
            Message.telegram_message_id == -2026091201,
        )
    )
    if source is None:
        source = Message(
            telegram_chat_id=chat_id,
            telegram_message_id=-2026091201,
            athlete_id=alexey.id,
            direction="configuration",
            message_type="configuration",
            raw_text=(
                "Confirmed by Alexey in Codex on 11–12 September 2026: personal tool for Alexey "
                "and Andrey; accurate training journal, next-session advice, progress reviews. "
                "Mostly listen; ask one useful missing-information question. Training facts may "
                "be shared; private personal context remains private. Trip tentatively late "
                "November, may move. Both have multipitch experience and will use a guide. "
                "Alexey aims to improve endurance and lead selected sections up to French 6a+. "
                "He currently has no gym partner, uses bouldering and autobelays, can train "
                "2–3 gym sessions/week and optional running or other conditioning. "
                "Andrey's individual grade goal has not been specified."
            ),
            processing_data={"origin": "user_confirmed_codex", "version": "20260912"},
            telegram_timestamp=datetime(2026, 9, 12, tzinfo=UTC),
        )
        session.add(source)
        await session.flush()
        for name, athlete in athletes.items():
            facts = {
                "multipitch_experience": "Previous multipitch climbing experience; current lead level unverified.",
                "trip": "Guided multipitch; lead selected sections. Tentative late November 2026, may move.",
            }
            if name == "Alexey":
                facts.update(
                    goal="Build endurance for guided multipitch days; aim to lead selected sections up to French 6a+.",
                    gym_access="No partner currently; bouldering and autobelays.",
                    availability="2–3 gym sessions/week; optional running or other conditioning.",
                )
            for field, value in facts.items():
                session.add(
                    Fact(
                        athlete_id=athlete.id,
                        field=field,
                        value=value,
                        source_message_id=source.id,
                        visibility="group",
                        status="active",
                    )
                )
            current_events = (
                await session.scalars(
                    select(Event).where(Event.athlete_id == athlete.id, Event.status == "active")
                )
            ).all()
            for event in current_events:
                event.status = "superseded"
            session.add(
                Event(
                    athlete_id=athlete.id,
                    name="Guided multipitch trip",
                    date=None,
                    date_precision="approximate",
                    shared_key="guided-multipitch",
                    visibility="group",
                    details={
                        "date_label": "late November 2026 (tentative; may move)",
                        "guided": True,
                        "route_type": "multipitch",
                        "role": "lead selected sections",
                    },
                    source_message_ids=[source.id],
                    evidence=["late november may move"],
                )
            )
            plans = (
                await session.scalars(
                    select(Plan).where(Plan.athlete_id == athlete.id, Plan.status == "active")
                )
            ).all()
            for p in plans:
                session.add(
                    Correction(
                        athlete_id=athlete.id,
                        source_message_id=source.id,
                        target_kind="plan",
                        target_id=p.id,
                        status="applied",
                        payload={"reason": "approved_planning_reset", "before": p.content},
                    )
                )
                p.status = "superseded"
            session.add(
                Plan(
                    athlete_id=athlete.id,
                    scope="program",
                    visibility="group",
                    start_date=date(2026, 9, 12),
                    end_date=None,
                    source_message_ids=[source.id],
                    evidence=[
                        "approved training constraints; schedule to be adapted in conversation"
                    ],
                    content={
                        "summary": "Основа подготовки — выносливость и экономичное лазание. Следующую сессию выбираем с учётом последних тренировок и восстановления.",
                        "current_focus": [
                            "climbing endurance",
                            "efficient movement",
                            "general endurance",
                        ],
                        "weekly_schedule": {},
                        "constraints": facts,
                        "schedule_status": "No fixed daily prescription; adapt to reported availability and recovery.",
                        "target_date_flexible": True,
                    },
                )
            )
    # The additive migration defaults historical free-form plans to "week".
    # Only the exact configuration-backed, undated baseline is a program; do
    # not reinterpret user-authored plans or revive superseded revisions.
    configured_plans = (
        await session.scalars(
            select(Plan).where(
                Plan.athlete_id.in_([athlete.id for athlete in athletes.values()]),
                Plan.scope == "week",
                Plan.end_date.is_(None),
            )
        )
    ).all()
    for plan in configured_plans:
        if (
            plan.source_message_ids == [source.id]
            and (plan.content or {}).get("schedule_status")
            == "No fixed daily prescription; adapt to reported availability and recovery."
        ):
            plan.scope = "program"
    await session.commit()
