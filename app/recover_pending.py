"""Replay messages left in `started` after an interrupted or broken extraction run."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import ReplyParameters
from sqlalchemy import select

from app.ai import AIService
from app.analytics import refresh_analytics_snapshots
from app.config import get_settings
from app.db.models import Athlete, Message, ProcessingRun
from app.db.session import Database
from app.memory import ContextBuilder, recent_message_text, semantic_history
from app.services import (
    MessageProcessor,
    is_directly_addressed,
    remove_inferred_numeric_ratings,
)
from app.telegram import format_telegram_reply

logger = logging.getLogger(__name__)

_STRUCTURED_HISTORY_CUE = re.compile(
    r"(?:\bV\d|\b5[.,]\d|\b\d?[5-9][abcабс][+-]?\b|лаз\w*|пролез\w*|"
    r"болдер\w*|трасс\w*|трениров\w*|памп\w*|бол\w*|устал\w*)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class PendingReply:
    chat_id: int
    telegram_message_id: int
    athlete_id: int
    text: str


def _source_text(message: Message) -> str:
    if message.raw_text:
        return message.raw_text
    if message.transcript:
        return message.transcript
    analysis = (message.attachment_data or {}).get("vision_analysis")
    return str(analysis or "Пользователь прислал вложение без доступного текста.")


async def recover() -> int:
    settings = get_settings()
    database = Database(settings.database_url, echo=settings.debug)
    ai = AIService(settings)
    processor = MessageProcessor(settings, ai, ContextBuilder(settings.recent_message_limit))
    pending_reply: PendingReply | None = None
    recovered = 0

    try:
        async with database.sessions() as session:
            rows = (
                await session.execute(
                    select(ProcessingRun, Message, Athlete)
                    .join(Message, ProcessingRun.message_id == Message.id)
                    .join(Athlete, Message.athlete_id == Athlete.id)
                    .where(ProcessingRun.status == "started")
                    .order_by(Message.telegram_timestamp, Message.id)
                )
            ).all()

            for run, message, athlete in rows:
                source_text = _source_text(message)
                local_today = message.telegram_timestamp.astimezone(
                    ZoneInfo(athlete.timezone)
                ).date()
                addressed = is_directly_addressed(
                    source_text,
                    allow_transcription_aliases=message.message_type == "voice",
                )
                try:
                    if not addressed and not _STRUCTURED_HISTORY_CUE.search(source_text):
                        message.normalized_text = source_text.strip()
                        run.extracted = {"recovery": "casual_message_no_structured_facts"}
                        run.mutations = []
                        run.status = "completed"
                        run.error = None
                        await session.commit()
                        recovered += 1
                        logger.info("Recovered casual message %s without extraction", message.id)
                        continue

                    recent = await recent_message_text(session, athlete.id, limit=12)
                    extraction = await ai.extract(
                        text=source_text,
                        athlete_name=athlete.name,
                        local_date=local_today.isoformat(),
                        timezone_name=athlete.timezone,
                        recent_context=recent,
                        directly_addressed=addressed,
                    )
                    remove_inferred_numeric_ratings(source_text, extraction)
                    message.normalized_text = extraction.normalized_text
                    run.extracted = extraction.model_dump(mode="json")
                    mutations = await processor._apply_extraction(
                        session,
                        athlete=athlete,
                        message=message,
                        extraction=extraction,
                        today=local_today,
                        source_text=source_text,
                    )
                    try:
                        message.embedding = await ai.embed(extraction.normalized_text)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Embedding recovery failed for message %s: %s", message.id, exc)
                        mutations.append({"kind": "embedding", "status": "failed"})

                    if addressed:
                        matches = await semantic_history(
                            session,
                            athlete_id=athlete.id,
                            query_embedding=message.embedding,
                            exclude_message_id=message.id,
                        )
                        context = await processor.context_builder.build(
                            session,
                            athlete=athlete,
                            today=local_today,
                            current_message=source_text,
                            semantic_matches=matches,
                            extraction_summary=json.dumps(
                                {
                                    "candidate": extraction.model_dump(
                                        mode="json", exclude_none=True
                                    ),
                                    "mutation_results": mutations,
                                },
                                ensure_ascii=False,
                            ),
                        )
                        reply = await ai.coach(
                            context=context,
                            safety_identifier=f"telegram-recovery-{athlete.id}",
                        )
                        pending_reply = PendingReply(
                            chat_id=message.telegram_chat_id,
                            telegram_message_id=message.telegram_message_id,
                            athlete_id=athlete.id,
                            text=reply,
                        )

                    run.mutations = mutations
                    run.status = "completed"
                    run.error = None
                    await refresh_analytics_snapshots(session, athlete.id, local_today)
                    await session.commit()
                    recovered += 1
                    logger.info("Recovered stored message %s", message.id)
                except Exception as exc:
                    await session.rollback()
                    failed_run = await session.get(ProcessingRun, run.id)
                    if failed_run:
                        failed_run.status = "failed"
                        failed_run.error = f"{type(exc).__name__}: {exc}"
                        await session.commit()
                    logger.exception("Could not recover stored message %s", message.id)

        if pending_reply is not None:
            bot = Bot(settings.telegram_bot_token)
            try:
                sent = await bot.send_message(
                    chat_id=pending_reply.chat_id,
                    text=format_telegram_reply(pending_reply.text),
                    parse_mode=ParseMode.HTML,
                    reply_parameters=ReplyParameters(
                        message_id=pending_reply.telegram_message_id
                    ),
                )
                async with database.sessions() as session:
                    await processor.save_outgoing(
                        session,
                        telegram_message_id=sent.message_id,
                        telegram_chat_id=sent.chat.id,
                        athlete_id=pending_reply.athlete_id,
                        text=pending_reply.text,
                        timestamp=sent.date or datetime.now(UTC),
                    )
            finally:
                await bot.session.close()
    finally:
        await database.dispose()

    return recovered


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    recovered = asyncio.run(recover())
    print(f"Recovered {recovered} pending messages")


if __name__ == "__main__":
    main()
