from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.enums import ChatType
from sqlalchemy import func, select

from app.ai import FakeAIService
from app.db.models import Athlete, Message, Plan, TrainingPreference
from app.onboarding import start_profile_onboarding
from app.planning import _step_lines
from app.schemas import (
    AvailabilityCandidate,
    CoachReply,
    EquipmentCandidate,
    ExperienceCandidate,
    LocationCandidate,
    MessageExtraction,
    PlanCandidate,
    PlannedBlockCandidate,
    PlanParameter,
    ProfileGoalCandidate,
    TrainingProfileCandidate,
)
from app.services import IncomingMessage, MessageProcessor
from app.telegram import TelegramRuntime
from app.training_commands import handle_training_command


def _incoming(message_id: int, text: str) -> IncomingMessage:
    return IncomingMessage(
        telegram_message_id=message_id,
        telegram_chat_id=303,
        telegram_user_id=303,
        timestamp=datetime(2026, 10, 21, 18, 0, tzinfo=UTC),
        text=text,
    )


async def _new_athlete(database) -> None:
    async with database.sessions() as session:
        athlete = Athlete(
            name="New athlete #303",
            telegram_user_id=303,
            timezone="America/Toronto",
            profile={},
        )
        session.add(athlete)
        await session.flush()
        preference = TrainingPreference(
            athlete_id=athlete.id,
            private_chat_id=303,
            profile={},
        )
        session.add(preference)
        start_profile_onboarding(
            preference, now=datetime(2026, 10, 21, 17, 55, tzinfo=UTC)
        )
        await session.commit()


async def test_onboarding_collects_profile_without_unsolicited_plan(database, settings):
    await _new_athlete(database)
    basics = MessageExtraction(
        normalized_text="Поддержание уровня, боулдеринг 6C, раз в неделю",
        conversation_intent="addressed",
        training_profile=TrainingProfileCandidate(
            goals=[
                ProfileGoalCandidate(
                    title="Поддержание уровня",
                    evidence=["Поддержание уровня"],
                )
            ],
            availability=[
                AvailabilityCandidate(day="раз в неделю", evidence=["раз в неделю"])
            ],
            experience=[
                ExperienceCandidate(
                    activity="bouldering",
                    flash_grade="6C",
                    evidence=["боулдеринг 6C"],
                )
            ],
        ),
    )
    first_ai = FakeAIService(basics, reply="This must not be used")
    async with database.sessions() as session:
        first = await MessageProcessor(settings, first_ai).process(
            session,
            _incoming(9001, "Поддержание уровня, боулдеринг 6C, раз в неделю"),
        )
        preference = await session.scalar(
            select(TrainingPreference).join(Athlete).where(Athlete.telegram_user_id == 303)
        )
        experience = preference.profile["experience"][0]
        assert experience["level"] == "6C"
        assert "flash_grade" not in experience
        assert (
            await session.scalar(
                select(func.count()).select_from(Plan).where(Plan.athlete_id == preference.athlete_id)
            )
            == 0
        )
    assert "Где обычно тренируешься" in first.reply
    assert first_ai.coach_contexts == []

    venue = MessageExtraction(
        normalized_text="Боулдеринговый зал, турник",
        conversation_intent="continuation",
        training_profile=TrainingProfileCandidate(
            locations=[
                LocationCandidate(name="climbing_gym", evidence=["Боулдеринговый зал"])
            ],
            equipment=[
                EquipmentCandidate(
                    name="pull-up bar", available=True, evidence=["турник"]
                )
            ],
        ),
    )
    second_ai = FakeAIService(venue, reply="This must not be used")
    async with database.sessions() as session:
        second = await MessageProcessor(settings, second_ai).process(
            session, _incoming(9002, "Боулдеринговый зал, турник")
        )
        preference = await session.scalar(
            select(TrainingPreference).join(Athlete).where(Athlete.telegram_user_id == 303)
        )
        assert preference.profile["_onboarding"]["status"] == "profile_ready"
        assert (
            await session.scalar(
                select(func.count()).select_from(Plan).where(Plan.athlete_id == preference.athlete_id)
            )
            == 0
        )
    assert "Следующий полезный шаг" in second.reply
    assert second_ai.coach_contexts == []


async def test_first_session_flow_requires_date_time_and_readiness(database, settings):
    await _new_athlete(database)
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 303))
        command = await handle_training_command(
            session=session,
            athlete=athlete,
            message=type(
                "PrivateCommand",
                (),
                {"raw_text": "/first", "telegram_chat_id": 303},
            )(),
            settings=settings,
            today=date(2026, 10, 21),
        )
        await session.commit()
    assert "когда она будет" in command.text

    ai = FakeAIService(
        MessageExtraction(
            normalized_text="В субботу",
            conversation_intent="continuation",
        ),
        reply="This must not be used",
    )
    async with database.sessions() as session:
        result = await MessageProcessor(settings, ai).process(
            session, _incoming(9003, "В субботу")
        )
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 303))
        assert (
            await session.scalar(
                select(func.count()).select_from(Plan).where(Plan.athlete_id == athlete.id)
            )
            == 0
        )
    assert "сколько минут" in result.reply
    assert "самочувствие" in result.reply
    assert ai.coach_contexts == []


async def test_explicit_first_session_handoff_creates_one_bounded_plan(database, settings):
    await _new_athlete(database)
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 303))
        await handle_training_command(
            session=session,
            athlete=athlete,
            message=type(
                "PrivateCommand",
                (),
                {"raw_text": "/first", "telegram_chat_id": 303},
            )(),
            settings=settings,
            today=date(2026, 10, 21),
        )
        await session.commit()

    class FirstPlanAI(FakeAIService):
        async def coach_reply(self, *, context, safety_identifier, repair=False):
            self.coach_contexts.append(context)
            return CoachReply(
                text="Собрал.",
                plan=PlanCandidate(
                    action="create",
                    scope="session",
                    start_date=date(2026, 10, 24),
                    end_date=date(2026, 10, 24),
                    location="climbing_gym",
                    title="Первая тренировка",
                    summary="Спокойная техническая сессия.",
                    purpose="Познакомиться с текущей формой без отказа.",
                    intensity="умеренная",
                    rest_guidance="Полный отдых между качественными попытками.",
                    blocks=[
                        PlannedBlockCandidate(
                            kind="ad_hoc",
                            title="Техническое лазание",
                            ad_hoc=True,
                            source="ad_hoc_noncatalog",
                            parameters=[
                                PlanParameter(
                                    name="duration_minutes", value=60, unit="min"
                                )
                            ],
                            instruction="Лезь с запасом и следи за постановкой ног.",
                        )
                    ],
                ),
            )

    ai = FirstPlanAI(
        MessageExtraction(
            normalized_text="В субботу, 90 минут, чувствую себя хорошо, боли нет",
            conversation_intent="continuation",
        )
    )
    async with database.sessions() as session:
        result = await MessageProcessor(settings, ai).process(
            session,
            _incoming(9004, "В субботу, 90 минут, чувствую себя хорошо, боли нет"),
        )
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 303))
        plans = (
            await session.scalars(select(Plan).where(Plan.athlete_id == athlete.id))
        ).all()
        preference = await session.get(TrainingPreference, athlete.id)
        assert len(plans) == 1
        assert plans[0].start_date == date(2026, 10, 24)
        assert preference.profile["_onboarding"]["status"] == "completed"
    assert "Тренировка на 24 октября" in result.reply
    assert "FIRST SESSION ONBOARDING" in ai.coach_contexts[0]


def test_catalog_step_labels_never_collapse_to_generic_exercise():
    lines = _step_lines(
        [
            {
                "type": "sequence",
                "label": "Complete exercises in order",
                "children": [
                    {
                        "type": "action",
                        "label": "Hanging knee raises",
                        "sets": 3,
                        "reps": 10,
                    },
                    {
                        "type": "action",
                        "label": "Bent-leg window wipers",
                        "sets": 3,
                        "reps": 10,
                    },
                ],
            }
        ],
        detailed=False,
        source_localized_only=True,
    )
    rendered = "\n".join(lines)
    assert "Подъёмы коленей в висе" in rendered
    assert "Дворники с согнутыми ногами" in rendered
    assert "Упражнение" not in rendered


async def test_first_session_button_queues_private_command(database, settings):
    runtime = TelegramRuntime(settings, database, SimpleNamespace())
    callback = SimpleNamespace(
        id="onboarding-first-1",
        data="onboard:first",
        from_user=SimpleNamespace(id=101),
        message=SimpleNamespace(
            message_id=700,
            chat=SimpleNamespace(id=101, type=ChatType.PRIVATE),
        ),
        answer=AsyncMock(),
    )
    try:
        await runtime._handle_callback(callback)
        async with database.sessions() as session:
            queued = await session.scalar(
                select(TrainingPreference).where(TrainingPreference.athlete_id == 1)
            )
            message = await session.scalar(select(Message).where(Message.raw_text == "/first"))
            assert queued is not None
            assert message.raw_text == "/first"
            assert message.processing_data["training_action"] == "first"
        callback.answer.assert_awaited_once_with("Запрос принят.")
    finally:
        await runtime.bot.session.close()

