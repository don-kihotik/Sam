from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Message as TelegramMessage
from pydantic import BaseModel
from sqlalchemy import select

from app.db.models import Athlete, Message, Outbox, ProcessingRun
from app.membership import ensure_preference
from app.telegram import TelegramRuntime


class ScopeResult(BaseModel):
    decision: str


class PolicyMediaAI:
    def __init__(self, decision: str = "allow"):
        self.decision = decision
        self.classify_scope = AsyncMock(return_value=ScopeResult(decision=decision))
        self.transcribe = AsyncMock()
        self.analyze_video_frames = AsyncMock()
        self.read_catalog_photo = AsyncMock()


def _video_message(*, message_id: int, caption: str, parent_user_id: int | None = None):
    payload = {
        "message_id": message_id,
        "date": 1790208000,
        "chat": {"id": 101, "type": "private", "first_name": "Alexey"},
        "from": {"id": 101, "is_bot": False, "first_name": "Alexey"},
        "caption": caption,
        "video": {
            "file_id": f"video-{message_id}",
            "file_unique_id": f"video-unique-{message_id}",
            "duration": 30,
            "width": 100,
            "height": 100,
        },
    }
    if parent_user_id is not None:
        payload["reply_to_message"] = {
            "message_id": message_id - 1,
            "date": 1790207900,
            "chat": {"id": 101, "type": "private"},
            "from": {"id": parent_user_id, "is_bot": False, "first_name": "Other"},
            "video": {
                "file_id": "parent-video",
                "file_unique_id": "parent-unique",
                "duration": 30,
                "width": 100,
                "height": 100,
            },
        }
        payload.pop("video")
        payload.pop("caption")
        payload["text"] = "Разбери технику"
    return TelegramMessage.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caption", "decision"),
    [
        ("Разбери технику другого спортсмена", "other_athlete"),
        ("Напиши код для сайта про скалолазание", "allow"),
    ],
)
async def test_policy_denied_captioned_video_never_downloads_or_analyzes(
    database, settings, caption, decision
):
    ai = PolicyMediaAI(decision)
    runtime = TelegramRuntime(settings, database, ai)
    runtime.bot.download = AsyncMock()
    try:
        await runtime._handle_message(_video_message(message_id=810, caption=caption))
        await runtime._process_next()
        runtime.bot.download.assert_not_called()
        ai.analyze_video_frames.assert_not_called()
        async with database.sessions() as session:
            run = await session.scalar(
                select(ProcessingRun).join(Message).where(Message.telegram_message_id == 810)
            )
            assert run.status == "failed" and run.error == "unsupported_media"
            assert await session.scalar(
                select(Outbox).where(Outbox.source_message_id == run.message_id)
            )
    finally:
        await runtime.bot.session.close()


@pytest.mark.asyncio
async def test_other_users_parent_video_is_rejected_before_download(database, settings):
    ai = PolicyMediaAI()
    runtime = TelegramRuntime(settings, database, ai)
    runtime.bot.download = AsyncMock()
    try:
        await runtime._handle_message(
            _video_message(message_id=811, caption="", parent_user_id=202)
        )
        await runtime._process_next()
        runtime.bot.download.assert_not_called()
        ai.classify_scope.assert_not_called()
    finally:
        await runtime.bot.session.close()


@pytest.mark.asyncio
async def test_overlong_voice_is_rejected_before_download_or_transcription(database, settings):
    ai = PolicyMediaAI()
    runtime = TelegramRuntime(settings, database, ai)
    runtime.bot.download = AsyncMock()
    voice = TelegramMessage.model_validate(
        {
            "message_id": 812,
            "date": 1790208000,
            "chat": {"id": 101, "type": "private", "first_name": "Alexey"},
            "from": {"id": 101, "is_bot": False, "first_name": "Alexey"},
            "voice": {"file_id": "long", "file_unique_id": "long-unique", "duration": 181},
        }
    )
    try:
        await runtime._handle_message(voice)
        await runtime._process_next()
        runtime.bot.download.assert_not_called()
        ai.transcribe.assert_not_called()
        ai.classify_scope.assert_not_called()
    finally:
        await runtime.bot.session.close()


@pytest.mark.asyncio
async def test_exhausted_voice_budget_stops_before_download_or_transcription(database, settings):
    settings.policy_hourly_requests = 1
    ai = PolicyMediaAI()
    runtime = TelegramRuntime(settings, database, ai)
    runtime.bot.download = AsyncMock()
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        session.add(
            Message(
                telegram_message_id=8130,
                telegram_chat_id=101,
                telegram_user_id=101,
                athlete_id=athlete.id,
                direction="incoming",
                message_type="text",
                telegram_timestamp=datetime.now(UTC),
                processing_data={
                    "policy_budget": [
                        {
                            "revision": 1,
                            "at": datetime.now(UTC).isoformat(),
                            "athlete_id": athlete.id,
                        }
                    ]
                },
            )
        )
        await session.commit()
    voice = TelegramMessage.model_validate(
        {
            "message_id": 813,
            "date": 1790208000,
            "chat": {"id": 101, "type": "private", "first_name": "Alexey"},
            "from": {"id": 101, "is_bot": False, "first_name": "Alexey"},
            "voice": {"file_id": "normal", "file_unique_id": "normal-unique", "duration": 20},
        }
    )
    try:
        await runtime._handle_message(voice)
        await runtime._process_next()
        runtime.bot.download.assert_not_called()
        ai.transcribe.assert_not_called()
        ai.classify_scope.assert_not_called()
    finally:
        await runtime.bot.session.close()


@pytest.mark.asyncio
async def test_edit_preserves_prior_budget_reservation_and_cannot_reset_quota(database, settings):
    settings.policy_hourly_requests = 1
    ai = PolicyMediaAI()
    runtime = TelegramRuntime(settings, database, ai)
    original = TelegramMessage.model_validate(
        {
            "message_id": 814,
            "date": 1790208000,
            "chat": {"id": 101, "type": "private", "first_name": "Alexey"},
            "from": {"id": 101, "is_bot": False, "first_name": "Alexey"},
            "text": "Первая версия отчёта",
        }
    )
    edited = TelegramMessage.model_validate(
        {
            "message_id": 814,
            "date": 1790208000,
            "edit_date": 1790208010,
            "chat": {"id": 101, "type": "private", "first_name": "Alexey"},
            "from": {"id": 101, "is_bot": False, "first_name": "Alexey"},
            "text": "Как улучшить мою тренировку?",
        }
    )
    try:
        await runtime._handle_message(original)
        async with database.sessions() as session:
            row = await session.scalar(select(Message).where(Message.telegram_message_id == 814))
            row.processing_data = {
                "policy_budget": [
                    {
                        "revision": 1,
                        "at": datetime.now(UTC).isoformat(),
                        "athlete_id": row.athlete_id,
                    }
                ]
            }
            await session.commit()
        await runtime._handle_message(edited)
        async with database.sessions() as session:
            row = await session.scalar(select(Message).where(Message.telegram_message_id == 814))
            assert row.revision == 2
            assert row.processing_data["policy_budget"][0]["revision"] == 1
        await runtime._process_next()
        async with database.sessions() as session:
            row = await session.scalar(select(Message).where(Message.telegram_message_id == 814))
            run = await session.scalar(
                select(ProcessingRun).where(ProcessingRun.message_id == row.id)
            )
            assert row.processing_data["policy_budget"][0]["revision"] == 1
            assert run.status == "completed"
            assert ai.classify_scope.await_count == 0
    finally:
        await runtime.bot.session.close()


@pytest.mark.asyncio
async def test_uncaptioned_circle_transcribes_then_scopes_transcript_without_vision(
    database, settings
):
    ai = PolicyMediaAI()

    async def classify_scope(*, text, context):
        # Empty caption would be unclear.  The actual spoken self-report is
        # what may authorize subsequent handling.
        return ScopeResult(decision="allow" if "моя" in text.lower() else "unclear")

    ai.classify_scope.side_effect = classify_scope
    ai.transcribe.return_value = "Моя тренировка: 4 боулдера, устали предплечья"
    runtime = TelegramRuntime(settings, database, ai)

    async def download(_media, destination):
        destination.write(b"circle")

    runtime.bot.download = AsyncMock(side_effect=download)
    circle = TelegramMessage.model_validate(
        {
            "message_id": 815,
            "date": 1790208000,
            "chat": {"id": 101, "type": "private", "first_name": "Alexey"},
            "from": {"id": 101, "is_bot": False, "first_name": "Alexey"},
            "video_note": {
                "file_id": "circle",
                "file_unique_id": "circle-unique",
                "length": 240,
                "duration": 12,
            },
        }
    )
    try:
        await runtime._handle_message(circle)
        async with database.sessions() as session:
            row = await session.scalar(select(Message).where(Message.telegram_message_id == 815))
            incoming = await runtime._prepare(session, circle, row)
            assert incoming is not None
            assert incoming.transcript == ai.transcribe.return_value
            assert incoming.message_type == "video_note"
        ai.transcribe.assert_awaited_once()
        ai.analyze_video_frames.assert_not_called()
        assert ai.classify_scope.await_count == 1
    finally:
        await runtime.bot.session.close()


@pytest.mark.asyncio
async def test_unaddressed_group_voice_offtopic_refusal_does_not_post_to_group(database, settings):
    ai = PolicyMediaAI()
    ai.transcribe.return_value = "Напиши код для сайта"
    runtime = TelegramRuntime(settings, database, ai)

    async def download(_media, destination):
        destination.write(b"voice")

    runtime.bot.download = AsyncMock(side_effect=download)
    voice = TelegramMessage.model_validate(
        {
            "message_id": 816,
            "date": 1790208000,
            "chat": {"id": -100123, "type": "supergroup", "title": "shared"},
            "from": {"id": 101, "is_bot": False, "first_name": "Alexey"},
            "voice": {
                "file_id": "group-voice",
                "file_unique_id": "group-voice-unique",
                "duration": 20,
            },
        }
    )
    try:
        async with database.sessions() as session:
            athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
            (await ensure_preference(session, athlete)).shared_chat_id = -100123
            await session.commit()
        await runtime._handle_message(voice)
        await runtime._process_next()
        async with database.sessions() as session:
            source = await session.scalar(select(Message).where(Message.telegram_message_id == 816))
            run = await session.scalar(
                select(ProcessingRun).where(ProcessingRun.message_id == source.id)
            )
            assert run.status == "failed" and run.error == "unsupported_media"
            assert (
                await session.scalar(select(Outbox).where(Outbox.source_message_id == source.id))
                is None
            )
    finally:
        await runtime.bot.session.close()


@pytest.mark.asyncio
async def test_directly_addressed_group_voice_offtopic_keeps_refusal(database, settings):
    ai = PolicyMediaAI()
    ai.transcribe.return_value = "Сэм, напиши код для сайта"
    runtime = TelegramRuntime(settings, database, ai)

    async def download(_media, destination):
        destination.write(b"voice")

    runtime.bot.download = AsyncMock(side_effect=download)
    voice = TelegramMessage.model_validate(
        {
            "message_id": 817,
            "date": 1790208000,
            "chat": {"id": -100123, "type": "supergroup", "title": "shared"},
            "from": {"id": 101, "is_bot": False, "first_name": "Alexey"},
            "voice": {
                "file_id": "direct-voice",
                "file_unique_id": "direct-voice-unique",
                "duration": 20,
            },
        }
    )
    try:
        async with database.sessions() as session:
            athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
            (await ensure_preference(session, athlete)).shared_chat_id = -100123
            await session.commit()
        await runtime._handle_message(voice)
        await runtime._process_next()
        async with database.sessions() as session:
            source = await session.scalar(select(Message).where(Message.telegram_message_id == 817))
            assert source.transcript == ai.transcribe.return_value
            assert await session.scalar(select(Outbox).where(Outbox.source_message_id == source.id))
    finally:
        await runtime.bot.session.close()
