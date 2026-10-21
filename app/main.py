from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.ai import AIService
from app.catalog import seed_catalog
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
        await seed_catalog(session)
        await session.commit()

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
async def health(request: Request) -> JSONResponse:
    telegram: TelegramRuntime | None = getattr(request.app.state, "telegram", None)
    telegram_health = telegram.health_details() if telegram else None
    database_ok = False
    queue = None
    try:
        async with asyncio.timeout(3):
            async with request.app.state.database.sessions() as session:
                await session.execute(text("SELECT 1"))
            database_ok = True
            if telegram:
                queue = await telegram.queue_health()
    except Exception as exc:  # noqa: BLE001 - health must remain available during outages
        logging.getLogger(__name__).warning("Readiness probe failed: %s", type(exc).__name__)
    healthy = bool(
        settings.telegram_ready
        and settings.openai_ready
        and telegram_health
        and telegram_health["polling_running"]
        and telegram_health["worker_running"]
        and database_ok
    )
    return JSONResponse(
        {
            "status": "ok" if healthy else "degraded",
            "telegram_configured": settings.telegram_ready,
            "openai_configured": settings.openai_ready,
            "telegram": telegram_health,
            "database_ok": database_ok,
            "queue": queue,
        },
        status_code=200 if healthy else 503,
    )
