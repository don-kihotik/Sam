from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.ai import FakeAIService
from app.analytics import analytics_windows
from app.db.models import AnalyticsSnapshot, Athlete, Event, Message, Plan, Workout
from app.memory import ContextBuilder, semantic_history
from app.schemas import (
    ClimbingEntry,
    CoachArtifacts,
    EventCandidate,
    MessageExtraction,
    PlanCandidate,
    WorkoutCandidate,
)
from app.services import IncomingMessage, MessageProcessor


def incoming(message_id: int, text: str) -> IncomingMessage:
    return IncomingMessage(
        telegram_message_id=message_id,
        telegram_chat_id=-100123,
        telegram_user_id=101,
        timestamp=datetime(2026, 9, 1, 20, 0, tzinfo=UTC),
        text=text,
    )


def september_workout(evidence: list[str]) -> MessageExtraction:
    return MessageExtraction(
        normalized_text="7 болдеров V1–V2 и 3×5.8, 2×5.9, 45 минут.",
        workout=WorkoutCandidate(
            present=True,
            date=date(2026, 9, 1),
            workout_type="climbing",
            duration_minutes=45,
            pain_status="unknown",
            climbing=[
                ClimbingEntry(
                    discipline="bouldering", grade_system="V", original_grade="V1–V2", count=7
                ),
                ClimbingEntry(
                    discipline="auto_belay", grade_system="YDS", original_grade="5.8", count=3
                ),
                ClimbingEntry(
                    discipline="auto_belay", grade_system="YDS", original_grade="5.9", count=2
                ),
            ],
            evidence=evidence,
        ),
    )


async def test_confirmation_cannot_duplicate_context_workout(database, settings):
    text = "Сегодня лазил 45 минут: 7 болдеров V1–V2, потом 3 по 5.8 и 2 по 5.9."
    async with database.sessions() as session:
        first = await MessageProcessor(
            settings, FakeAIService(september_workout(["Сегодня лазил 45 минут"]))
        ).process(session, incoming(100, text))
        second = await MessageProcessor(
            settings, FakeAIService(september_workout(["Сегодня лазил 45 минут"]))
        ).process(session, incoming(101, "Сэм, ты это записал?"))
        active_count = await session.scalar(
            select(func.count(Workout.id)).where(Workout.status == "active")
        )
        workout = await session.scalar(
            select(Workout)
            .options(selectinload(Workout.entries))
            .where(Workout.date == date(2026, 9, 1))
        )

    assert first.mutations and first.mutations[0]["status"] == "created"
    assert second.mutations and second.mutations[0]["status"] == "rejected_no_current_evidence"
    assert active_count == 2  # imported baseline plus the real September session
    assert workout is not None and len(workout.entries) == 3
    assert workout.pain_status == "unknown"


async def test_analytics_are_calculated_from_normalized_entries(database, settings):
    text = "Сегодня лазил 45 минут: 7 болдеров V1–V2, потом 3 по 5.8 и 2 по 5.9."
    async with database.sessions() as session:
        await MessageProcessor(
            settings, FakeAIService(september_workout(["Сегодня лазил 45 минут"]))
        ).process(session, incoming(102, text))
        metrics = await analytics_windows(session, 1, date(2026, 9, 1))
        snapshot = await session.scalar(
            select(AnalyticsSnapshot).where(
                AnalyticsSnapshot.athlete_id == 1,
                AnalyticsSnapshot.period_end == date(2026, 9, 1),
                AnalyticsSnapshot.window_days == 7,
            )
        )

    assert metrics["7"]["sessions"] == 1
    assert metrics["7"]["duration_minutes"] == 45
    assert metrics["7"]["total_climbs"] == 12
    assert metrics["7"]["pain_unknown_sessions"] == 1
    assert metrics["7"]["pain_free_sessions"] == 0
    assert metrics["all_time"]["sessions"] == 2
    assert snapshot is not None and snapshot.metrics["total_climbs"] == 12


async def test_explicit_event_move_supersedes_old_target(database, settings):
    extraction = MessageExtraction(
        normalized_text="Поездка теперь где-то в конце ноября.",
        event=EventCandidate(
            action="update",
            name="Guided multipitch climbing trip",
            date_precision="approximate",
            date_label="late November 2026",
            route_type="multipitch",
            guided=True,
            evidence=["где-то в конце ноября"],
        ),
    )
    async with database.sessions() as session:
        await MessageProcessor(settings, FakeAIService(extraction)).process(
            session, incoming(103, "Сэм, поездка теперь где-то в конце ноября")
        )
        active = (
            await session.scalars(
                select(Event).where(Event.athlete_id == 1, Event.status == "active")
            )
        ).all()

    assert len(active) == 1
    assert active[0].date is None
    assert active[0].details["date_label"] == "late November 2026"


class PlanAI(FakeAIService):
    async def extract_coach_artifacts(self, text: str) -> CoachArtifacts:
        return CoachArtifacts(
            plan=PlanCandidate(
                action="update",
                summary="Plan for this week",
                weekly_schedule={"Monday": "rest", "Tuesday": "easy climbing"},
                evidence=["Пн — отдых; Вт — лёгкое лазание"],
            )
        )

    async def embed(self, text: str) -> list[float]:
        return [1.0] * 1536


async def test_outgoing_plan_is_persisted_and_embedded(database, settings):
    processor = MessageProcessor(settings, PlanAI(MessageExtraction(normalized_text="")))
    text = "План на неделю: Пн — отдых; Вт — лёгкое лазание; Ср — аэробика; Чт — техника."
    async with database.sessions() as session:
        message = await processor.save_outgoing(
            session,
            telegram_message_id=500,
            telegram_chat_id=-100123,
            athlete_id=1,
            text=text,
        )
        plan = await session.scalar(
            select(Plan)
            .where(Plan.athlete_id == 1, Plan.status == "active")
            .order_by(Plan.created_at.desc())
        )

    assert message.embedding is not None
    assert plan is not None and plan.content["weekly_schedule"]["Monday"] == "rest"


async def test_semantic_history_excludes_current_message(database):
    async with database.sessions() as session:
        session.add_all(
            [
                Message(
                    telegram_message_id=700,
                    telegram_chat_id=-100123,
                    telegram_user_id=101,
                    athlete_id=1,
                    direction="incoming",
                    message_type="text",
                    raw_text="current",
                    normalized_text="current",
                    embedding=[1.0] * 1536,
                    telegram_timestamp=datetime.now(UTC),
                ),
                Message(
                    telegram_message_id=701,
                    telegram_chat_id=-100123,
                    telegram_user_id=None,
                    athlete_id=1,
                    direction="outgoing",
                    message_type="text",
                    raw_text="saved plan",
                    normalized_text="saved plan",
                    embedding=[0.9] * 1536,
                    telegram_timestamp=datetime.now(UTC),
                ),
            ]
        )
        await session.flush()
        current_id = await session.scalar(
            select(Message.id).where(Message.telegram_message_id == 700)
        )
        matches = await semantic_history(
            session,
            athlete_id=1,
            query_embedding=[1.0] * 1536,
            exclude_message_id=current_id,
        )

    assert "current" not in matches
    assert "saved plan" in matches


async def test_memory_audit_context_contains_canonical_truth(database):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.id == 1))
        context = await ContextBuilder().build(
            session,
            athlete=athlete,
            today=date(2026, 9, 1),
            current_message="Сэм, что ты помнишь и сколько раз я лазил?",
            semantic_matches=[],
            extraction_summary="{}",
        )

    assert "late November 2026" in context
    assert "'all_time': {'sessions': 1}" in context
    assert "pain_status=reported_none" in context
    assert "weekly_schedule" in context
