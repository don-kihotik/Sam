from sqlalchemy import select

from app.db.models import Athlete, Event, Plan
from app.db.session import Database


async def test_seeded_state_survives_database_reopen(database, settings):
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey is not None
        assert alexey.profile["physical"]["height_cm"] == 193
        assert await session.scalar(select(Event).where(Event.athlete_id == alexey.id))
        assert await session.scalar(select(Plan).where(Plan.athlete_id == alexey.id))

    await database.dispose()
    reopened = Database(settings.database_url)
    async with reopened.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey is not None
        assert alexey.telegram_user_id == 101
    await reopened.dispose()
