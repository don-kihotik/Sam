from datetime import UTC, date, datetime

from sqlalchemy import func, select

from app.ai import FakeAIService
from app.db.models import Athlete, BacklogItem, Correction, Message, Workout
from app.schemas import (
    BacklogCandidate,
    ClimbingEntry,
    CorrectionCandidate,
    DailyStateCandidate,
    MemoryCandidate,
    MessageExtraction,
    NamedScore,
    WorkoutCandidate,
)
from app.services import (
    IncomingMessage,
    MessageProcessor,
    is_directly_addressed,
    remove_inferred_numeric_ratings,
)


def incoming(message_id: int, user_id: int, text: str) -> IncomingMessage:
    return IncomingMessage(
        telegram_message_id=message_id,
        telegram_chat_id=-100123,
        telegram_user_id=user_id,
        timestamp=datetime(2026, 8, 30, 16, 0, tzinfo=UTC),
        text=text,
    )


async def test_workout_memory_and_reply_are_personalized(database, settings):
    extraction = MessageExtraction(
        normalized_text="Сегодня сделал 3 маршрута 5.9, забитость 6/10.",
        should_respond=False,
        workout=WorkoutCandidate(
            present=True,
            date=date(2026, 8, 30),
            workout_type="climbing",
            pump=6,
            climbing=[
                ClimbingEntry(
                    discipline="auto_belay",
                    grade_system="YDS",
                    original_grade="5.9",
                    count=3,
                    completed=True,
                )
            ],
            evidence=["сегодня сделал три 5.9"],
        ),
        memory=MemoryCandidate(
            create=True,
            kind="episodic",
            summary="Alexey completed three 5.9 auto-belay routes with pump 6/10.",
            tags=["climbing", "pump"],
            evidence=["сегодня сделал три 5.9"],
        ),
    )
    ai = FakeAIService(extraction, reply="Норм. Записал три 5.9, забитость 6/10.")
    processor = MessageProcessor(settings, ai)

    async with database.sessions() as session:
        result = await processor.process(
            session,
            incoming(1, 101, "Сэм, сегодня сделал три 5.9, забитость на шесть"),
        )
        workout = await session.scalar(select(Workout).where(Workout.date == date(2026, 8, 30)))

    assert result.reply == "Норм. Записал три 5.9, забитость 6/10."
    assert result.athlete_id is not None
    assert workout is not None
    assert workout.structured_details["pump"] == 6
    assert workout.structured_details["climbing"][0]["original_grade"] == "5.9"
    assert "Alexey" in ai.coach_contexts[0]
    assert "Andrey" not in ai.coach_contexts[0]


async def test_duplicate_telegram_delivery_is_idempotent(database, settings):
    extraction = MessageExtraction(normalized_text="Привет", should_respond=True)
    processor = MessageProcessor(settings, FakeAIService(extraction))
    update = incoming(7, 101, "Сэм, привет")

    async with database.sessions() as session:
        first = await processor.process(session, update)
        second = await processor.process(session, update)
        count = await session.scalar(select(func.count(Message.id)))

    assert first.duplicate is False
    assert second.duplicate is True
    assert count == 1


async def test_unknown_user_is_not_persisted(database, settings):
    processor = MessageProcessor(
        settings, FakeAIService(MessageExtraction(normalized_text="Сэм, привет"))
    )
    async with database.sessions() as session:
        result = await processor.process(session, incoming(8, 999, "Сэм, привет"))
        count = await session.scalar(select(func.count(Message.id)))
    assert result.ignored_unknown_user is True
    assert count == 0


async def test_explicit_backlog_creation(database, settings):
    extraction = MessageExtraction(
        normalized_text="Добавь в backlog weekly review.",
        backlog=BacklogCandidate(
            action="create",
            item_type="improvement",
            title="Добавить нормальный weekly review",
            description="Сравнивать объём и нагрузку по неделям.",
        ),
    )
    processor = MessageProcessor(settings, FakeAIService(extraction, reply="Записал."))
    async with database.sessions() as session:
        result = await processor.process(
            session, incoming(9, 101, "Сэм, добавь в backlog нормальный weekly review")
        )
        item = await session.scalar(select(BacklogItem))
    assert result.reply == "Записал."
    assert item is not None
    assert item.type == "improvement"
    assert item.original_message_id == result.message_id


async def test_correction_can_reassign_workout(database, settings):
    initial = MessageExtraction(
        normalized_text="Сегодня была тренировка.",
        workout=WorkoutCandidate(
            present=True,
            date=date(2026, 8, 30),
            workout_type="climbing",
            evidence=["Сегодня полазил"],
        ),
    )
    async with database.sessions() as session:
        await MessageProcessor(settings, FakeAIService(initial)).process(
            session, incoming(10, 101, "Сегодня полазил")
        )

    correction = MessageExtraction(
        normalized_text="Это Андрей лазил.",
        corrections=[
            CorrectionCandidate(
                target_kind="workout",
                target_date=date(2026, 8, 30),
                correct_athlete_name="Andrey",
            )
        ],
    )
    async with database.sessions() as session:
        await MessageProcessor(settings, FakeAIService(correction)).process(
            session, incoming(11, 101, "Нет, это Андрей лазил")
        )
        workout = await session.scalar(select(Workout).where(Workout.date == date(2026, 8, 30)))
        andrey = await session.scalar(select(Athlete).where(Athlete.name == "Andrey"))
        audit = await session.scalar(select(Correction))
    assert workout is not None and andrey is not None
    assert workout.athlete_id == andrey.id
    assert audit is not None and audit.status == "applied"


async def test_non_addressed_message_can_be_silently_learned(database, settings):
    extraction = MessageExtraction(
        normalized_text="Плохо спал, энергии 4/10.",
        should_respond=False,
        daily_state=DailyStateCandidate(
            present=True, energy=4, sleep_quality=3, evidence=["Плохо спал сегодня"]
        ),
    )
    processor = MessageProcessor(settings, FakeAIService(extraction))
    async with database.sessions() as session:
        result = await processor.process(session, incoming(12, 101, "Плохо спал сегодня"))
    assert result.reply is None
    assert result.mutations and result.mutations[0]["kind"] == "daily_state"


def test_direct_address_detection():
    assert is_directly_addressed("Сэм, что сегодня?")
    assert is_directly_addressed("/today")
    assert is_directly_addressed("обычный ответ", is_reply_to_sam=True)
    assert not is_directly_addressed("завтра идём лазить")
    assert is_directly_addressed(
        "Короче, Санта, для тебя голосом запишу",
        allow_transcription_aliases=True,
    )
    assert not is_directly_addressed("Санта завтра идёт лазить")


def test_qualitative_fatigue_is_not_converted_to_numeric_score():
    extraction = MessageExtraction(
        normalized_text="Сделал 7 трасс и сильно забился.",
        workout=WorkoutCandidate(present=True, pump=7),
        daily_state=DailyStateCandidate(
            present=True,
            general_fatigue=7,
            soreness=[NamedScore(area="предплечья", value=7)],
        ),
    )

    removed = remove_inferred_numeric_ratings(
        "Сделал 7 трасс V1–V2, мало отдыхал и сильно забился.", extraction
    )

    assert extraction.workout.pump is None
    assert extraction.daily_state.general_fatigue is None
    assert extraction.daily_state.soreness == []
    assert removed


def test_explicit_numeric_rating_is_preserved():
    extraction = MessageExtraction(
        normalized_text="Забитость 6/10, боль в пальце на три.",
        workout=WorkoutCandidate(
            present=True,
            pump=6,
            pain=[NamedScore(area="палец", value=3)],
        ),
    )

    remove_inferred_numeric_ratings("Забитость 6/10, боль в пальце на три.", extraction)

    assert extraction.workout.pump == 6
    assert extraction.workout.pain[0].value == 3
