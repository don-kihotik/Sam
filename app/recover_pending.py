"""Explicitly requeue failed work; the normal durable worker handles extraction and delivery."""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import select

from app.config import get_settings
from app.db.models import Outbox, ProcessingRun
from app.db.session import Database


async def recover(apply: bool = False) -> dict:
    db = Database(get_settings().database_url)
    try:
        async with db.sessions() as session:
            runs = (
                await session.scalars(select(ProcessingRun).where(ProcessingRun.status == "failed"))
            ).all()
            replies = (await session.scalars(select(Outbox).where(Outbox.status == "failed"))).all()
            result = {
                "applied": apply,
                "messages": [r.message_id for r in runs],
                "replies": [r.id for r in replies],
            }
            if apply:
                for run in runs:
                    if run.error == "unsupported_media":
                        continue
                    run.status, run.attempts, run.next_attempt_at = "received", 0, None
                for reply in replies:
                    reply.status, reply.attempts, reply.next_attempt_at = "retry", 0, None
                await session.commit()
            return result
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(asyncio.run(recover(parser.parse_args().apply))))
