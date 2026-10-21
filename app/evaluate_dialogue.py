"""Opt-in bounded real-model replay in a disposable DB, never Telegram/production.

python -m app.evaluate_dialogue [--show-replies]
Uses current credentials/models, but replaces DB and Telegram identities.
"""

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.ai import AIService
from app.catalog import seed_catalog
from app.config import get_settings
from app.db.models import Plan, ProcessingRun, Workout
from app.db.session import Database
from app.planning import session_duration_minutes
from app.seed import seed_initial_data
from app.services import IncomingMessage, MessageProcessor


async def replay(show_replies: bool) -> None:
    live = get_settings()
    if not live.openai_ready:
        raise SystemExit("OpenAI credentials are not configured")
    with tempfile.TemporaryDirectory(prefix="sam-dialogue-") as folder:
        settings = live.model_copy(update={
            "database_url": "sqlite+aiosqlite:///" + str(Path(folder) / "replay.sqlite3"),
            "telegram_enabled": False, "telegram_bot_token": "123456:synthetic",
            "alexey_telegram_user_id": 101, "andrey_telegram_user_id": 202,
            "telegram_allowed_chat_id": -100123, "embeddings_enabled": False,
        })
        db = Database(settings.database_url)
        await db.create_all_for_tests()
        ai = AIService(settings)
        try:
            async with db.sessions() as session:
                await seed_initial_data(session, settings)
                await seed_catalog(session)
                await session.commit()
                original_workouts = list(await session.scalars(select(Workout.id)))
                cases = [
                    ("start", "Привет! Давай спланируем дальше тренировки. Я правда чуть приболел на этой неделе, но, надеюсь, смогу полазать в воскресенье."),
                    ("sunday", "Что посоветуешь как тренировку в воскресенье"),
                    ("combine", "Могу комбинировать"),
                    ("access", "В воскресенье могу и болдеринг и с автостраховкой полазать"),
                    ("exercises", "Какие упражнения посоветуешь?"),
                    ("shorten", "Сократи это занятие до 30 минут."),
                    ("reschedule", "Перенеси это занятие на пятницу 2026-10-02."),
                ]
                processor = MessageProcessor(settings, ai)
                failures = []
                for index, (label, request) in enumerate(cases):
                    when = datetime(2026, 9, 24, 20, tzinfo=UTC) + timedelta(minutes=index)
                    incoming = IncomingMessage(telegram_message_id=9900+index, telegram_chat_id=101,
                        telegram_user_id=101, timestamp=when, text=request)
                    result = await processor.process(session, incoming)
                    if result.reply:
                        await processor.save_outgoing(session, telegram_message_id=19900+index,
                            telegram_chat_id=101, athlete_id=1, text=result.reply,
                            reply_to_message_id=incoming.telegram_message_id, timestamp=when)
                    plans = list(await session.scalars(select(Plan).where(
                        Plan.athlete_id == 1, Plan.scope == "session", Plan.status == "active")))
                    run = await session.scalar(select(ProcessingRun).where(ProcessingRun.message_id == result.message_id))
                    errors = [item.get("reason") for item in run.extracted.get("coaching_diagnostics", []) if item.get("status") == "rejected"]
                    expected_date = date(2026,10,2) if label == "reschedule" else date(2026,9,27)
                    ok = bool(result.reply) and all(p.start_date == expected_date for p in plans)
                    ok = ok and len(plans) <= 1 and "Не смог надёжно" not in (result.reply or "")
                    if label in {"exercises", "shorten", "reschedule"}:
                        ok = ok and len(plans) == 1 and bool(plans[0].content.get("blocks"))
                    if label == "shorten" and plans:
                        minutes = session_duration_minutes(plans[0].content.get('blocks', []))
                        ok = ok and minutes is not None and minutes <= 30
                    ok = ok and list(await session.scalars(select(Workout.id))) == original_workouts
                    if not ok:
                        failures.append(label)
                    print(json.dumps({"case":label, "pass":ok, "active_sessions":len(plans),
                        "dates":[str(p.start_date) for p in plans], "repair_reasons":errors,
                        "reply":result.reply if show_replies else None}, ensure_ascii=False), flush=True)
                if failures:
                    raise SystemExit("Failed dialogue cases: " + ", ".join(failures))
                print("PASS: full seven-turn replay; no production writes or Telegram sends", flush=True)
        finally:
            await ai.client.close()
            await db.dispose()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show-replies", action="store_true")
    asyncio.run(replay(parser.parse_args().show_replies))
