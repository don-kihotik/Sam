from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from app.ai import FakeAIService
from app.db.models import Athlete, Message, Plan, ProcessingRun, Workout
from app.memory import ContextBuilder
from app.schemas import MessageExtraction
from app.services import IncomingMessage, MessageProcessor


class DeterministicOnlyAI:
    """Fail loudly if a command unexpectedly falls through to model extraction."""

    async def __getattr__(self, _name):  # pragma: no cover - Python never calls this form
        raise AssertionError("deterministic command invoked AI")

    def __getattribute__(self, name):
        if name.startswith("_"):
            return object.__getattribute__(self, name)
        raise AssertionError(f"deterministic command invoked AI.{name}")


async def _durable_callback(
    session, processor: MessageProcessor, *, plan_id: int, callback_id: int
) -> IncomingMessage:
    incoming = IncomingMessage(
        telegram_message_id=-callback_id,
        telegram_chat_id=101,
        telegram_user_id=101,
        timestamp=datetime(2026, 9, 24, 12, tzinfo=UTC),
        text=f"/confirm {plan_id}",
        message_type="callback",
        attachment_metadata={
            "callback_id": f"cb-{callback_id}",
            "callback_data": f"train:confirm:{plan_id}",
        },
    )
    recorded = await processor.record_unprocessed(
        session, incoming, status="received", reason="callback"
    )
    message = await session.get(Message, recorded.message_id)
    message.processing_data = {"training_action": "confirm_plan", "plan_id": plan_id}
    await session.commit()
    return incoming


@pytest.mark.asyncio
async def test_private_weight_never_enters_group_model_context(database, settings):
    """A BodyMeasurement has only a source Message, so source visibility is the guard."""
    text = "PRIVATE_WEIGHT_CANARY: сегодня 55 кг"
    extraction = MessageExtraction(
        normalized_text=text,
        weight_kg=55.0,
        weight_evidence=["55 кг"],
    )
    incoming = IncomingMessage(
        telegram_message_id=9901,
        telegram_chat_id=101,
        telegram_user_id=101,
        timestamp=datetime(2026, 9, 24, tzinfo=UTC),
        text=text,
    )
    async with database.sessions() as session:
        await MessageProcessor(settings, FakeAIService(extraction)).process(session, incoming)
        athlete = await session.get(Athlete, 1)
        group_context = await ContextBuilder(group_chat_id=-100123).build(
            session,
            athlete=athlete,
            today=date(2026, 9, 24),
            current_message="progress",
            semantic_matches=[],
            extraction_summary="{}",
            chat_id=-100123,
        )
    assert "55.0" not in group_context


@pytest.mark.asyncio
async def test_foreign_group_sources_do_not_become_shared_group_context(database):
    """Only the configured shared chat, not any negative chat ID, is shareable."""
    async with database.sessions() as session:
        athlete = await session.get(Athlete, 1)
        source = Message(
            telegram_message_id=9902,
            telegram_chat_id=-777001,
            telegram_user_id=101,
            athlete_id=athlete.id,
            raw_text="foreign group report",
            message_type="text",
            telegram_timestamp=datetime(2026, 9, 24, tzinfo=UTC),
        )
        session.add(source)
        await session.flush()
        session.add(
            Workout(
                athlete_id=athlete.id,
                date=date(2026, 9, 24),
                type="training",
                duration_minutes=None,
                rpe=None,
                notes=None,
                structured_details={
                    "activities": [
                        {"kind": "strength", "exercise": "FOREIGN_GROUP_CANARY", "sets": []}
                    ]
                },
                source_message_ids=[source.id],
            )
        )
        await session.flush()
        group_context = await ContextBuilder(group_chat_id=-100123).build(
            session,
            athlete=athlete,
            today=date(2026, 9, 24),
            current_message="progress",
            semantic_matches=[],
            extraction_summary="{}",
            chat_id=-100123,
        )
    assert "FOREIGN_GROUP_CANARY" not in group_context


@pytest.mark.asyncio
async def test_durable_log_callback_confirms_once_and_typed_confirm_cannot_forge(
    database, settings
):
    """Only the authenticated callback metadata can consume an open log authorization."""
    processor = MessageProcessor(settings, DeterministicOnlyAI())
    day = date(2026, 9, 24)
    async with database.sessions() as session:
        athlete = await session.get(Athlete, 1)
        plan = Plan(
            athlete_id=athlete.id,
            scope="session",
            start_date=day,
            end_date=day,
            content={
                "summary": "Numeric session",
                "blocks": [
                    {
                        "kind": "strength",
                        "title": "Hangboard",
                        "parameters": [
                            {"name": "sets", "value": 3},
                            {"name": "reps", "value": 5},
                            {"name": "weight_kg", "value": 10, "unit": "kg"},
                        ],
                    }
                ],
            },
        )
        session.add(plan)
        await session.commit()

        log = IncomingMessage(
            telegram_message_id=9910,
            telegram_chat_id=101,
            telegram_user_id=101,
            timestamp=datetime(2026, 9, 24, 11, tzinfo=UTC),
            text=f"/log {plan.id}",
        )
        await processor.process(session, log)

        forged = IncomingMessage(
            telegram_message_id=9911,
            telegram_chat_id=101,
            telegram_user_id=101,
            timestamp=datetime(2026, 9, 24, 11, 1, tzinfo=UTC),
            text=f"/confirm {plan.id}",
        )
        await processor.process(session, forged)
        assert await session.scalar(select(func.count()).select_from(Workout)) == 1

        callback = await _durable_callback(session, processor, plan_id=plan.id, callback_id=9912)
        confirmed = await processor.process(session, callback)
        assert confirmed.reply and "Записал фактическую" in confirmed.reply
        assert await session.scalar(select(func.count()).select_from(Workout)) == 2
        workout = await session.scalar(select(Workout).where(Workout.plan_id == plan.id))
        assert workout.structured_details["activities"][0]["sets"][0]["weight_kg"] == 10

        # Telegram's durable callback identity is already completed, so this
        # replay returns without re-running the command or duplicating metrics.
        duplicate = await processor.process(session, callback)
        assert duplicate.duplicate
        assert await session.scalar(select(func.count()).select_from(Workout)) == 2

        # Reopening the log cannot revive the consumed confirmation grant.
        reopened_log = IncomingMessage(
            telegram_message_id=9914,
            telegram_chat_id=101,
            telegram_user_id=101,
            timestamp=datetime(2026, 9, 24, 12, 5, tzinfo=UTC),
            text=f"/log {plan.id}",
        )
        await processor.process(session, reopened_log)
        second_callback = await _durable_callback(
            session, processor, plan_id=plan.id, callback_id=9913
        )
        second = await processor.process(session, second_callback)
        assert second.reply and "уже записана" in second.reply
        assert await session.scalar(select(func.count()).select_from(Workout)) == 2
        run = await session.scalar(
            select(ProcessingRun).join(Message).where(Message.telegram_message_id == -9912)
        )
        assert run.status == "completed"
