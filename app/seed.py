from datetime import date
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import grade_rank
from app.config import Settings
from app.db.models import Athlete, Event, Message, Plan, Workout, WorkoutEntry
from app.maintenance import canonicalize_history

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


async def _upsert_athlete(
    session: AsyncSession,
    *,
    name: str,
    telegram_user_id: int | None,
    timezone_name: str,
    profile: dict,
) -> Athlete:
    athlete = await session.scalar(select(Athlete).where(Athlete.name == name))
    if athlete is None:
        athlete = Athlete(name=name, profile=profile, timezone=timezone_name)
        session.add(athlete)
        await session.flush()
    if telegram_user_id is not None:
        athlete.telegram_user_id = telegram_user_id
    athlete.timezone = timezone_name
    if name == "Alexey":
        existing_profile = dict(athlete.profile or profile)
        goals = existing_profile.get("goals", [])
        if not goals or any("October" in str(goal) for goal in goals):
            existing_profile["goals"] = profile["goals"]
        for key, value in profile.items():
            existing_profile.setdefault(key, value)
        athlete.profile = existing_profile
    return athlete


async def seed_initial_data(session: AsyncSession, settings: Settings) -> None:
    alexey = await _upsert_athlete(
        session,
        name="Alexey",
        telegram_user_id=settings.alexey_telegram_user_id,
        timezone_name=settings.timezone,
        profile=ALEXEY_PROFILE,
    )
    await _upsert_athlete(
        session,
        name="Andrey",
        telegram_user_id=settings.andrey_telegram_user_id,
        timezone_name=settings.timezone,
        profile={"identity": {"name": "Andrey"}},
    )

    event_source = await session.scalar(
        select(Message)
        .where(
            Message.athlete_id == alexey.id,
            Message.direction == "incoming",
            Message.normalized_text.ilike("%конец ноября%"),
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    plan_source = await session.scalar(
        select(Message)
        .where(
            Message.athlete_id == alexey.id,
            Message.direction == "outgoing",
            Message.normalized_text.ilike("%На ближайшую неделю%"),
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )

    event = await session.scalar(
        select(Event).where(Event.athlete_id == alexey.id, Event.status == "active")
    )
    if event is None:
        session.add(
            Event(
                athlete_id=alexey.id,
                name="Guided multipitch climbing trip",
                date=None,
                date_precision="approximate",
                details={
                    "date_label": "late November 2026",
                    "route_type": "multipitch",
                    "guided": True,
                },
                evidence=["где-то конец ноября"],
                source_message_ids=[event_source.id] if event_source else [],
            )
        )
    elif event.date == date(2026, 10, 15) and event.details.get("date_is_approximate"):
        event.date = None
        event.date_precision = "approximate"
        event.details = {
            "date_label": "late November 2026",
            "route_type": "multipitch",
            "guided": True,
        }
        event.evidence = ["где-то конец ноября"]
        event.source_message_ids = [event_source.id] if event_source else []

    plan = await session.scalar(
        select(Plan).where(Plan.athlete_id == alexey.id, Plan.status == "active")
    )
    initial_plan = {
        "status": "initial hypothesis",
        "summary": "Набрать комфортный объём и горную выносливость к концу ноября.",
        "current_focus": [
            "комфортный объём лазания",
            "ровный темп и техника",
            "общая горная выносливость",
        ],
        "weekly_schedule": {
            "Monday": "отдых или ходьба 30–60 минут",
            "Tuesday": "лёгкое-среднее лазание 60–90 минут; больше V1–V2 и немного V3 без отказа",
            "Wednesday": "аэробика 30–45 минут и мобилити",
            "Thursday": "ровный темп и техника без жёстких проектов",
            "Friday": "отдых",
            "Saturday": "длинная спокойная сессия на стене или скалах",
            "Sunday": "лёгкая прогулка или восстановление",
        },
        "preferred_climbing_days": ["Tuesday", "Thursday", "weekend"],
        "guardrails": [
            "avoid rapid finger-load progression after the long break",
            "adapt daily to pain and recovery",
        ],
    }
    if plan is None:
        session.add(
            Plan(
                athlete_id=alexey.id,
                start_date=date(2026, 8, 30),
                end_date=date(2026, 11, 30),
                content=initial_plan,
                evidence=["На ближайшую неделю я бы сделал так"],
                source_message_ids=[plan_source.id] if plan_source else [],
            )
        )
    elif plan.content.get("status") == "initial hypothesis" and (
        plan.end_date == date(2026, 10, 15)
        or (plan_source is not None and plan.source_message_ids == [plan_source.id])
    ):
        plan.end_date = date(2026, 11, 30)
        plan.content = initial_plan
        plan.evidence = ["На ближайшую неделю я бы сделал так"]
        plan.source_message_ids = [plan_source.id] if plan_source else []

    baseline_fingerprint = sha256(b"alexey-baseline-2026-08").hexdigest()
    baseline = await session.scalar(
        select(Workout).where(
            Workout.athlete_id == alexey.id,
            Workout.fingerprint == baseline_fingerprint,
            Workout.status == "active",
        )
    )
    if baseline is None:
        entries = [
            ("bouldering", "V", "V1", 2, 2),
            ("bouldering", "V", "V2", 3, 3),
            ("bouldering", "V", "V3", 3, 2),
            ("bouldering", "V", "V4", 1, 0),
            ("auto_belay", "YDS", "5.8", 2, 2),
            ("auto_belay", "YDS", "5.9", 6, 6),
        ]
        baseline = Workout(
            athlete_id=alexey.id,
            date=None,
            date_precision="month",
            type="climbing",
            duration_minutes=None,
            rpe=None,
            notes="Baseline session reported during initial product setup; exact date unknown.",
            structured_details={
                "pump": 6.5,
                "pain": [],
                "reserve": "meaningful",
                "date_label": "August 2026",
            },
            pain_status="reported_none",
            evidence=["baseline imported from the agreed athlete profile"],
            fingerprint=baseline_fingerprint,
            source_message_ids=[],
            entries=[
                WorkoutEntry(
                    discipline=discipline,
                    grade_system=system,
                    original_grade=original,
                    grade_rank=grade_rank(system, original),
                    count=count,
                    completed_count=completed,
                )
                for discipline, system, original, count, completed in entries
            ],
        )
        session.add(baseline)
    await session.flush()
    await canonicalize_history(session)
    await session.commit()
