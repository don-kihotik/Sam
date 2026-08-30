from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Athlete, Event, Plan

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
    "goals": ["Prepare physically for a guided multipitch trip in mid-October 2026"],
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
    if name == "Alexey" and not athlete.profile:
        athlete.profile = profile
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

    event = await session.scalar(
        select(Event).where(Event.athlete_id == alexey.id, Event.status == "active")
    )
    if event is None:
        session.add(
            Event(
                athlete_id=alexey.id,
                name="Guided multipitch climbing trip",
                date=date(2026, 10, 15),
                details={"date_is_approximate": True, "route_type": "multipitch", "guided": True},
            )
        )

    plan = await session.scalar(
        select(Plan).where(Plan.athlete_id == alexey.id, Plan.status == "active")
    )
    if plan is None:
        session.add(
            Plan(
                athlete_id=alexey.id,
                start_date=date(2026, 8, 30),
                end_date=date(2026, 10, 15),
                content={
                    "status": "initial hypothesis",
                    "current_focus": [
                        "return to climbing movement",
                        "build comfortable route volume",
                        "aerobic mountain conditioning",
                    ],
                    "preferred_climbing_days": ["Tuesday", "Thursday", "weekend"],
                    "guardrails": [
                        "avoid rapid finger-load progression after the long break",
                        "adapt daily to pain and recovery",
                    ],
                },
            )
        )
    await session.commit()
