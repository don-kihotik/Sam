from pathlib import Path

import pytest

from app.config import Settings
from app.db.session import Database
from app.seed import seed_initial_data


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}",
        openai_api_key="test",
        telegram_allowed_chat_id=-100123,
        alexey_telegram_user_id=101,
        andrey_telegram_user_id=202,
        embeddings_enabled=False,
    )


@pytest.fixture
async def database(settings: Settings):
    database = Database(settings.database_url)
    await database.create_all_for_tests()
    async with database.sessions() as session:
        await seed_initial_data(session, settings)
    yield database
    await database.dispose()
