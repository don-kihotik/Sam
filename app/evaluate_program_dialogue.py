"""Replay the exact two-turn monthly-plan conversation in disposable SQLite.

Uses configured live models, synthetic Telegram identities, no Telegram delivery,
and never connects to the production database.
"""

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
from app.db.models import Plan, ProcessingRun, TrainingPreference, Workout
from app.db.session import Database
from app.seed import seed_initial_data
from app.services import IncomingMessage, MessageProcessor


async def replay() -> None:
    live = get_settings()
    if not live.openai_ready:
        raise SystemExit("OpenAI credentials are not configured")
    with tempfile.TemporaryDirectory(prefix="sam-program-dialogue-") as folder:
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
                before_workouts = list(await session.scalars(select(Workout.id)))
                processor = MessageProcessor(settings, ai)
                requests = [
                    "собери мне детальный план тренировок на ближайший месяц",
                    ("три тренировки в неделю, пока еще есть простуда, начну в это воскресенье, "
                     "буду в скалодроме, миксовать болдеринг и лазание с верхней страховкой"),
                ]
                results = []
                for index, request in enumerate(requests):
                    when = datetime(2026, 9, 24, 22, tzinfo=UTC) + timedelta(minutes=index)
                    result = await processor.process(session, IncomingMessage(
                        telegram_message_id=9800 + index, telegram_chat_id=101,
                        telegram_user_id=101, timestamp=when, text=request,
                    ))
                    results.append(result)
                    if result.reply:
                        await processor.save_outgoing(
                            session, telegram_message_id=19800 + index,
                            telegram_chat_id=101, athlete_id=1, text=result.reply,
                            reply_to_message_id=9800 + index, timestamp=when,
                        )
                programs = list(await session.scalars(select(Plan).where(
                    Plan.athlete_id == 1, Plan.scope == "program", Plan.status == "active",
                )))
                sessions = list(await session.scalars(select(Plan).where(
                    Plan.athlete_id == 1, Plan.scope == "session", Plan.status == "active",
                )))
                task = (await session.get(TrainingPreference, 1)).profile["_open_planning_tasks"][-1]
                run = await session.scalar(select(ProcessingRun).where(
                    ProcessingRun.message_id == results[-1].message_id,
                ))
                diagnostics = run.extracted.get("coaching_diagnostics", [])
                program = programs[0] if len(programs) == 1 else None
                days = (program.end_date - program.start_date).days + 1 if program and program.end_date else 0
                # A program may be either a reusable weekly template or include
                # explicit dated child sessions.  The latter are useful for a
                # detailed month plan, but every child must be actionable and
                # remain inside the parent program's date range.
                sessions_ok = bool(
                    program
                    and all(
                        child.parent_id == program.id
                        and program.start_date <= child.start_date <= program.end_date
                        and bool((child.content or {}).get("blocks"))
                        for child in sessions
                    )
                )
                ok = bool(
                    program
                    and program.start_date == date(2026, 9, 27)
                    and 21 <= days <= 45
                    and len((program.content or {}).get("phases", [])) >= 2
                    and len((program.content or {}).get("weekly_schedule", {})) >= 3
                    and sessions_ok
                    and set(program.source_message_ids) == {results[0].message_id, results[1].message_id}
                    and task["status"] == "completed"
                    and list(await session.scalars(select(Workout.id))) == before_workouts
                    and "Не смог надёжно" not in (results[-1].reply or "")
                )
                print(json.dumps({
                    "pass": ok, "first_reply": results[0].reply,
                    "final_reply": results[-1].reply, "active_programs": len(programs),
                    "active_program_rows": [
                        {"id": p.id, "start": str(p.start_date), "supersedes": p.supersedes_id,
                         "sources": p.source_message_ids, "visibility": p.visibility}
                        for p in programs
                    ],
                    "program_dates": [str(program.start_date), str(program.end_date)] if program else None,
                    "task_status": task["status"],
                    "materialized_sessions": len(sessions),
                    "materialized_session_rows": [
                        {
                            "date": str(child.start_date),
                            "title": (child.content or {}).get("summary"),
                            "block_count": len((child.content or {}).get("blocks", [])),
                        }
                        for child in sessions
                    ],
                    "repair_reasons": [d.get("reason") for d in diagnostics if d.get("status") == "rejected"],
                }, ensure_ascii=False), flush=True)
                if not ok:
                    raise SystemExit("FAIL: monthly program conversation")
                print("PASS: exact monthly-program follow-up retained and completed", flush=True)
        finally:
            await ai.client.close()
            await db.dispose()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(replay())
