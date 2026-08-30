from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.ai import AIService
from app.config import get_settings
from app.db.session import Database
from app.seed import seed_initial_data
from app.telegram import TelegramRuntime

settings = get_settings()
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database = Database(settings.database_url, echo=settings.debug)
    app.state.database = database
    if database.engine.dialect.name == "sqlite":
        await database.create_all_for_tests()
    async with database.sessions() as session:
        await seed_initial_data(session, settings)

    telegram: TelegramRuntime | None = None
    if settings.telegram_ready and settings.openai_ready:
        telegram = TelegramRuntime(settings, database, AIService(settings))
        await telegram.start()
    else:
        logging.getLogger(__name__).warning(
            "Telegram polling disabled: configure bot token, allowed chat ID and OpenAI key"
        )
    app.state.telegram = telegram
    yield
    if telegram:
        await telegram.stop()
    await database.dispose()


app = FastAPI(title="Sam", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "telegram_configured": settings.telegram_ready,
        "openai_configured": settings.openai_ready,
    }
