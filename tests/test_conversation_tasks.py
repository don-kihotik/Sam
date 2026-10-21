from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app.ai import FakeAIService
from app.coaching import validate_planning_task_candidate
from app.conversation_tasks import complete_planning_task, sync_open_planning_task
from app.db.models import Athlete, Message, Plan, TrainingPreference
from app.schemas import (
    CoachReply,
    MessageExtraction,
    PlanCandidate,
    PlanPhaseCandidate,
    WeeklyScheduleItem,
)
from app.services import IncomingMessage, MessageProcessor

NOW = datetime(2026, 9, 24, 22, tzinfo=UTC)


async def _message(session, athlete_id, number, text, *, chat=101, when=NOW):
    row = Message(
        telegram_message_id=number, telegram_chat_id=chat, telegram_user_id=101,
        athlete_id=athlete_id, direction="incoming", message_type="text",
        raw_text=text, telegram_timestamp=when,
        processing_data={"request_policy": {"decision": "allow"}},
    )
    session.add(row)
    await session.flush()
    return row


async def test_task_persists_root_and_completes_without_leaking_between_chats(database):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        preference = await session.get(TrainingPreference, athlete.id)
        root = await _message(session, athlete.id, 9301, "Собери детальный план на ближайший месяц")
        task = await sync_open_planning_task(
            session, preference=preference, message=root, now=NOW,
        )
        assert task and task.requested_scope == "program" and not task.ready

        answer = await _message(
            session, athlete.id, 9302,
            "Три тренировки в неделю, начну в воскресенье, буду в скалодроме",
            when=NOW + timedelta(minutes=1),
        )
        task = await sync_open_planning_task(
            session, preference=preference, message=answer, now=NOW + timedelta(minutes=1),
        )
        assert task and task.ready and task.source_message_ids == (root.id, answer.id)
        assert "детальный план" in task.model_context() and "Три тренировки" in task.model_context()

        other_chat = await _message(
            session, athlete.id, 9303, "Да, три раза", chat=999,
            when=NOW + timedelta(minutes=2),
        )
        assert await sync_open_planning_task(
            session, preference=preference, message=other_chat, now=NOW + timedelta(minutes=2),
        ) is None
        complete_planning_task(preference, task, plan_id=77)
        assert preference.profile["_open_planning_tasks"][-1]["status"] == "completed"


async def test_dated_session_detour_does_not_consume_open_program(database):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        preference = await session.get(TrainingPreference, athlete.id)
        root = await _message(session, athlete.id, 9311, "План на ближайший месяц")
        task = await sync_open_planning_task(session, preference=preference, message=root, now=NOW)
        detour = await _message(
            session, athlete.id, 9312, "Составь тренировку на сегодня",
            when=NOW + timedelta(minutes=1),
        )
        assert await sync_open_planning_task(
            session, preference=preference, message=detour, now=NOW + timedelta(minutes=1),
        ) is None
        stored = preference.profile["_open_planning_tasks"][-1]
        assert stored["status"] == "open" and stored["source_message_ids"] == [task.root_message_id]


def _program() -> PlanCandidate:
    return PlanCandidate(
        action="create", scope="program", start_date=date(2026, 9, 27),
        end_date=date(2026, 10, 24), summary="Месяц развития выносливости",
        phases=[
            PlanPhaseCandidate(title="Возврат", start_date=date(2026, 9, 27),
                               end_date=date(2026, 10, 3), objective="Спокойный объём"),
            PlanPhaseCandidate(title="Развитие", start_date=date(2026, 10, 4),
                               end_date=date(2026, 10, 24), objective="Выносливость"),
        ],
        weekly_schedule=[
            WeeklyScheduleItem(day="Tuesday", activity="болдеринг: техника"),
            WeeklyScheduleItem(day="Thursday", activity="трассы: объём"),
            WeeklyScheduleItem(day="Sunday", activity="болдеринг и верхняя страховка: объём"),
        ],
        guardrails=["Первая неделя условная: при сохраняющемся недомогании снизить нагрузку."],
    )


async def test_russian_detailed_program_rejects_english_placeholders(database):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        preference = await session.get(TrainingPreference, athlete.id)
        root = await _message(session, athlete.id, 9321, "Собери детальный план на ближайший месяц")
        await sync_open_planning_task(session, preference=preference, message=root, now=NOW)
        answer = await _message(
            session, athlete.id, 9322, "Три тренировки в неделю в скалодроме",
            when=NOW + timedelta(minutes=1),
        )
        task = await sync_open_planning_task(
            session, preference=preference, message=answer, now=NOW + timedelta(minutes=1),
        )
        candidate = _program()
        candidate.weekly_schedule[0].activity = "Training session"
        try:
            validate_planning_task_candidate(candidate, task)
        except ValueError as exc:
            assert "descriptive weekly activities" in str(exc)
        else:
            raise AssertionError("English placeholder was accepted")


class TaskDialogueAI(FakeAIService):
    def __init__(self):
        super().__init__(MessageExtraction(normalized_text=""))
        self.turn = 0
        self.calls = 0

    async def extract(self, *, text, **kwargs):
        return MessageExtraction(normalized_text=text, should_respond=True,
                                 conversation_intent="continuation" if self.turn else "clarification")

    async def coach_reply(self, *, context, **kwargs):
        self.coach_contexts.append(context)
        self.calls += 1
        if self.turn == 0:
            self.turn += 1
            return CoachReply(
                text="Сколько раз в неделю и где будешь тренироваться?",
                follow_up_question="Сколько раз в неделю и где будешь тренироваться?",
            )
        if self.calls == 2:
            return CoachReply(text="Вот воскресенье", plan=PlanCandidate(
                action="create", scope="session", start_date=date(2026, 9, 27),
                end_date=date(2026, 9, 27), summary="Одна сессия",
            ))
        return CoachReply(text="Готово", plan=_program())


async def test_real_month_dialogue_repairs_session_scope_into_program(database, settings):
    ai = TaskDialogueAI()
    processor = MessageProcessor(settings, ai)
    async with database.sessions() as session:
        first = await processor.process(session, IncomingMessage(
            telegram_message_id=9401, telegram_chat_id=101, telegram_user_id=101,
            timestamp=NOW, text="собери мне детальный план тренировок на ближайший месяц",
        ))
        assert "Сколько раз" in first.reply
        second = await processor.process(session, IncomingMessage(
            telegram_message_id=9402, telegram_chat_id=101, telegram_user_id=101,
            timestamp=NOW + timedelta(minutes=1),
            text=("три тренировки в неделю, пока еще есть простуда, начну в это воскресенье, "
                  "буду в скалодроме, миксовать болдеринг и лазание с верхней страховкой"),
        ))
        programs = list(await session.scalars(select(Plan).where(
            Plan.athlete_id == 1, Plan.scope == "program", Plan.status == "active",
        )))
        sessions = list(await session.scalars(select(Plan).where(
            Plan.athlete_id == 1, Plan.scope == "session", Plan.status == "active",
        )))
        assert len(programs) == 1
        assert programs[0].start_date == date(2026, 9, 27)
        assert set(programs[0].source_message_ids) == {first.message_id, second.message_id}
        assert "Программа" in second.reply and "Месяц развития" in second.reply
        assert "OPEN PLANNING TASK" in ai.coach_contexts[-1]
        assert sessions == []  # a program template must not create undetailed reminders
        preference = await session.get(TrainingPreference, 1)
        assert preference.profile["_open_planning_tasks"][-1]["status"] == "completed"
        assert ai.calls == 3
