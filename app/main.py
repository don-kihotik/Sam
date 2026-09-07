from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

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
async def health(request: Request) -> dict:
    telegram: TelegramRuntime | None = getattr(request.app.state, "telegram", None)
    telegram_health = telegram.health_details() if telegram else None
    healthy = bool(
        settings.telegram_ready
        and settings.openai_ready
        and telegram_health
        and telegram_health["polling_running"]
        and not telegram_health["last_error"]
    )
    return {
        "status": "ok" if healthy else "degraded",
        "telegram_configured": settings.telegram_ready,
        "openai_configured": settings.openai_ready,
        "telegram": telegram_health,
    }
