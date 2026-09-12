from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.ai import FakeAIService
from app.analytics import analytics_windows
from app.db.models import Athlete, FollowUp, Memory, Message, Outbox, Plan, ProcessingRun, Workout
from app.delivery import split_reply
from app.memory import ContextBuilder, recent_message_text
from app.schemas import (
    ClimbingEntry,
    CoachReply,
    MemoryCandidate,
    MessageExtraction,
    PlanCandidate,
    WeeklyScheduleItem,
    WorkoutCandidate,
)
from app.services import IncomingMessage, MessageProcessor, is_directly_addressed
from app.telegram import TelegramRuntime


def incoming(n, text, *, user=101, chat=-100123, seconds=0):
    return IncomingMessage(
        telegram_message_id=n,
        telegram_chat_id=chat,
        telegram_user_id=user,
        timestamp=datetime(2026, 9, 12, 16, tzinfo=UTC) + timedelta(seconds=seconds),
        text=text,
    )


def workout(text, *, grade=None, count=None, minutes=None, action="auto", target=None):
    return MessageExtraction(
        normalized_text=text,
        workout=WorkoutCandidate(
            present=True,
            date=date(2026, 9, 12),
            workout_type="climbing",
            session_action=action,
            target_workout_id=target,
            duration_minutes=minutes,
            evidence=[text],
            climbing=[
                ClimbingEntry(
                    discipline="bouldering",
                    grade_system="V",
                    original_grade=grade,
                    count=count,
                    completed=True,
                    evidence=[text],
                )
            ]
            if grade
            else [],
        ),
    )


async def test_fragmented_session_and_explicit_second_session(database, settings):
    first = "Сделал 2 V2"
    second = "А ещё 3 V1"
    async with database.sessions() as s:
        a = await MessageProcessor(
            settings, FakeAIService(workout(first, grade="V2", count=2))
        ).process(s, incoming(201, first))
        b = await MessageProcessor(
            settings, FakeAIService(workout(second, grade="V1", count=3))
        ).process(s, incoming(202, second, seconds=41))
        third = "Вторая тренировка: сделал 2 V2"
        await MessageProcessor(
            settings, FakeAIService(workout(third, grade="V2", count=2, action="new"))
        ).process(s, incoming(203, third, seconds=4000))
        stats = await analytics_windows(s, 1, date(2026, 9, 12))
    assert a.mutations[0]["id"] == b.mutations[0]["id"]
    assert stats["7"]["sessions"] == 2
    assert stats["7"]["total_climbs"] == 7


async def test_easy_bouldering_cannot_invent_one_climb(database, settings):
    text = "Лазил сегодня, болдеринг легкие трассы, минут 40 после работы"
    extraction = workout(text, grade="легкие трассы", count=1, minutes=40)
    async with database.sessions() as s:
        result = await MessageProcessor(settings, FakeAIService(extraction)).process(
            s, incoming(210, text)
        )
        row = await s.get(Workout, result.mutations[0]["id"])
        stats = await analytics_windows(s, 1, date(2026, 9, 12))
    assert row.duration_minutes == 40
    assert row.entries[0].count is None
    assert row.entries[0].original_grade is None
    assert stats["7"]["unknown_count_entries"] == 1


async def test_banter_cannot_create_memory_even_if_model_proposes_it(database, settings):
    text = "не-не. теперь не отвертишься. с тебя теперь 6с :))"
    extraction = MessageExtraction(
        normalized_text=text,
        is_banter=True,
        should_respond=True,
        memory=MemoryCandidate(create=True, kind="coach", summary="Goal 6c", evidence=[text]),
    )
    async with database.sessions() as s:
        result = await MessageProcessor(settings, FakeAIService(extraction)).process(
            s, incoming(211, text, user=202)
        )
        memories = await s.scalar(select(func.count()).select_from(Memory))
    assert result.reply is None
    assert memories == 0


def test_talking_about_sam_is_not_a_direct_address():
    assert not is_directly_addressed("Andrey, Sam got that wrong.")
    assert not is_directly_addressed("Андрюх, у Сэма опять проблема")
    assert is_directly_addressed("Сэм, что сегодня?")


async def test_private_context_excluded_but_training_numbers_shared(database, settings):
    private = "Сегодня 40 минут лазил. PRIVATE_CANARY family reason"
    extraction = workout(private, minutes=40)
    extraction.workout.notes = "PRIVATE_CANARY"
    extraction.memory = MemoryCandidate(
        create=True, summary="PRIVATE_CANARY", evidence=["PRIVATE_CANARY"]
    )
    async with database.sessions() as s:
        await MessageProcessor(settings, FakeAIService(extraction)).process(
            s, incoming(212, private, chat=101)
        )
        athlete = await s.get(Athlete, 1)
        builder = ContextBuilder(group_chat_id=-100123)
        group = await builder.build(
            s,
            athlete=athlete,
            today=date(2026, 9, 12),
            current_message="progress",
            semantic_matches=[],
            extraction_summary="{}",
            chat_id=-100123,
        )
        personal = await builder.build(
            s,
            athlete=athlete,
            today=date(2026, 9, 12),
            current_message="progress",
            semantic_matches=[],
            extraction_summary="{}",
            chat_id=101,
        )
    assert "PRIVATE_CANARY" not in group
    assert '"duration_minutes": 40' in group
    assert "PRIVATE_CANARY" in personal


async def test_group_history_contains_both_named_speakers(database, settings):
    async with database.sessions() as s:
        for n, user, text in [(215, 101, "Мои трассы короткие"), (216, 202, "Мои по 17 метров")]:
            await MessageProcessor(
                settings, FakeAIService(MessageExtraction(normalized_text=text))
            ).process(s, incoming(n, text, user=user))
        history = await recent_message_text(s, 1, chat_id=-100123)
    assert "speaker=Alexey" in history and "speaker=Andrey" in history


class FailingAI(FakeAIService):
    async def extract(self, **kwargs):
        raise RuntimeError("synthetic failure")


async def test_failed_extraction_is_retryable_and_error_is_not_masked(database, settings):
    msg = incoming(220, "Сэм, привет")
    async with database.sessions() as s:
        with pytest.raises(RuntimeError, match="synthetic failure"):
            await MessageProcessor(
                settings, FailingAI(MessageExtraction(normalized_text=msg.text))
            ).process(s, msg)
        run = await s.scalar(
            select(ProcessingRun).join(Message).where(Message.telegram_message_id == 220)
        )
        assert run.status == "retry"
        result = await MessageProcessor(
            settings, FakeAIService(MessageExtraction(normalized_text=msg.text))
        ).process(s, msg)
        count = await s.scalar(select(func.count()).select_from(Outbox))
    assert not result.duplicate and result.reply
    assert count == 1


class PlannedAI(FakeAIService):
    async def coach_reply(self, **kwargs):
        return CoachReply(
            text="На этой неделе так.",
            plan=PlanCandidate(
                action="update",
                summary="Endurance week",
                weekly_schedule=[
                    WeeklyScheduleItem(day="Tuesday", activity="easy autobelay volume")
                ],
            ),
        )


async def test_reply_and_plan_saved_atomically_before_delivery(database, settings):
    async with database.sessions() as s:
        result = await MessageProcessor(
            settings, PlannedAI(MessageExtraction(normalized_text="plan"))
        ).process(s, incoming(230, "Сэм, составь план"))
        plan = await s.scalar(select(Plan).where(Plan.athlete_id == 1, Plan.status == "active"))
        queued = await s.scalar(select(Outbox))
    assert plan.content["weekly_schedule"]["Tuesday"] == "easy autobelay volume"
    assert "easy autobelay volume" in result.reply
    assert queued.text == result.reply and queued.status == "pending"


async def test_followup_updates_original_session(database, settings):
    text = "Сегодня лазил 40 минут"
    async with database.sessions() as s:
        result = await MessageProcessor(settings, FakeAIService(workout(text, minutes=40))).process(
            s, incoming(240, text)
        )
        wid = result.mutations[0]["id"]
        s.add(
            FollowUp(
                athlete_id=1,
                telegram_chat_id=-100123,
                source_message_id=result.message_id,
                workout_id=wid,
                question="Как ощущения?",
                expires_at=datetime(2026, 9, 14, tzinfo=UTC),
            )
        )
        await s.commit()
        reply = "Нормально, запас остался"
        ext = workout(reply)
        ext.answers_follow_up = True
        ext.workout.notes = reply
        await MessageProcessor(settings, FakeAIService(ext)).process(
            s, incoming(241, reply, seconds=60)
        )
        rows = (await s.scalars(select(Workout).where(Workout.date == date(2026, 9, 12)))).all()
        q = await s.scalar(select(FollowUp))
    assert len(rows) == 1 and rows[0].duration_minutes == 40
    assert reply in rows[0].notes and q.status == "answered"


async def test_edit_replaces_only_its_contribution(database, settings):
    first, second = "Сделал 2 V2", "А ещё 3 V1"
    async with database.sessions() as s:
        await MessageProcessor(
            settings, FakeAIService(workout(first, grade="V2", count=2))
        ).process(s, incoming(250, first))
        await MessageProcessor(
            settings, FakeAIService(workout(second, grade="V1", count=3))
        ).process(s, incoming(251, second, seconds=41))
        edit = incoming(250, "Сделал 4 V2")
        edit.is_edit = True
        await MessageProcessor(
            settings, FakeAIService(workout(edit.text, grade="V2", count=4, action="append"))
        ).process(s, edit)
        stats = await analytics_windows(s, 1, date(2026, 9, 12))
    assert stats["7"]["sessions"] == 1
    assert stats["7"]["total_climbs"] == 7


def test_long_reply_splits_within_utf16_limit():
    parts = split_reply("• **Пн** — session 🙂\n" * 1000)
    assert len(parts) > 1
    assert all(len(p.encode("utf-16-le")) // 2 <= 3500 for p in parts)


async def test_outbox_retries_delivery_without_reprocessing_workout(database, settings):
    async with database.sessions() as s:
        await MessageProcessor(
            settings, FakeAIService(MessageExtraction(normalized_text="hello"))
        ).process(s, incoming(260, "Сэм, привет"))
    runtime = TelegramRuntime(
        settings, database, FakeAIService(MessageExtraction(normalized_text="unused"))
    )
    runtime.bot.send_message = AsyncMock(
        side_effect=[
            RuntimeError("network"),
            SimpleNamespace(message_id=999, date=datetime.now(UTC)),
        ]
    )
    try:
        await runtime._deliver_next()
        async with database.sessions() as s:
            item = await s.scalar(select(Outbox))
            assert item.status == "retry"
            item.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            await s.commit()
        await runtime._deliver_next()
        async with database.sessions() as s:
            item = await s.scalar(select(Outbox))
            assert item.status == "sent" and item.sent_message_ids == [999]
    finally:
        await runtime.bot.session.close()


async def test_voice_intake_is_durable_before_download_failure(database, settings):
    from aiogram.types import Message as TelegramMessage

    runtime = TelegramRuntime(
        settings, database, FakeAIService(MessageExtraction(normalized_text="unused"))
    )
    msg = TelegramMessage.model_validate(
        {
            "message_id": 270,
            "date": 1789228800,
            "chat": {"id": -100123, "type": "supergroup", "title": "test"},
            "from": {"id": 101, "is_bot": False, "first_name": "Alexey"},
            "voice": {"file_id": "test_voice", "file_unique_id": "u1", "duration": 10},
        }
    )
    runtime.bot.download = AsyncMock(side_effect=RuntimeError("download unavailable"))
    try:
        await runtime._handle_message(msg)
        async with database.sessions() as s:
            row = await s.scalar(select(Message).where(Message.telegram_message_id == 270))
            assert row.attachment_data["telegram_payload"]["voice"]["file_id"] == "test_voice"
        await runtime._process_next()
        async with database.sessions() as s:
            run = await s.scalar(
                select(ProcessingRun).join(Message).where(Message.telegram_message_id == 270)
            )
            assert run.status == "retry" and run.error == "RuntimeError"
    finally:
        await runtime.bot.session.close()


async def test_oversized_video_is_explained_without_download(database, settings):
    from aiogram.types import Message as TelegramMessage

    runtime = TelegramRuntime(
        settings, database, FakeAIService(MessageExtraction(normalized_text="unused"))
    )
    msg = TelegramMessage.model_validate(
        {
            "message_id": 271,
            "date": 1789228800,
            "chat": {"id": 101, "type": "private"},
            "from": {"id": 101, "is_bot": False, "first_name": "Alexey"},
            "video": {
                "file_id": "test_video",
                "file_unique_id": "u2",
                "duration": 241,
                "width": 480,
                "height": 640,
                "file_size": 25000000,
            },
        }
    )
    runtime.bot.download = AsyncMock()
    try:
        await runtime._handle_message(msg)
        await runtime._process_next()
        async with database.sessions() as s:
            queued = await s.scalar(select(Outbox))
            assert "20 MB" in queued.text and queued.status == "pending"
        runtime.bot.download.assert_not_called()
    finally:
        await runtime.bot.session.close()


async def test_more_than_hundred_sessions_have_complete_analytics_and_paged_history(
    database, settings
):
    from app.memory import render_history_audit

    async with database.sessions() as s:
        s.add_all(
            [
                Workout(
                    athlete_id=1,
                    date=date(2026, 9, 12),
                    type="bouldering",
                    duration_minutes=40,
                    source_message_ids=[],
                    entries=[],
                )
                for _ in range(120)
            ]
        )
        await s.flush()
        stats = await analytics_windows(s, 1, date(2026, 9, 12))
        athlete = await s.get(Athlete, 1)
        report = await render_history_audit(s, athlete=athlete, today=date(2026, 9, 12), offset=100)
    assert stats["7"]["sessions"] == 120 and stats["7"]["duration_minutes"] == 4800
    assert "/history 110" in report and report.count("• #") == 10
