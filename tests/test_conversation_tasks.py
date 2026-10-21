from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.ai import FakeAIService
from app.coaching import validate_detailed_plan, validate_planning_task_candidate
from app.conversation_tasks import complete_planning_task, sync_open_planning_task
from app.db.models import Athlete, Message, Plan, TrainingPreference
from app.planning import save_plan
from app.schemas import (
    CoachReply,
    MessageExtraction,
    PlanCandidate,
    PlannedBlockCandidate,
    PlannedSessionCandidate,
    PlanParameter,
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


def test_detailed_week_rejects_schedule_labels_without_executable_session_cards():
    candidate = PlanCandidate(
        action="create",
        scope="week",
        start_date=date(2026, 10, 21),
        weekly_schedule=[WeeklyScheduleItem(day="Wednesday", activity="Лёгкое лазание")],
    )
    try:
        validate_detailed_plan(candidate, required=True)
    except ValueError as exc:
        assert "dated sessions" in str(exc)
    else:
        raise AssertionError("A schedule-only detailed week was accepted")


@pytest.mark.parametrize(
    ("parameter"),
    [
        PlanParameter(name="duration_minutes", value=0, unit="min"),
        PlanParameter(name="duration_minutes", value=True, unit="min"),
        PlanParameter(name="duration_minutes", value="10", unit="min"),
        PlanParameter(name="duration_minutes", value=10),
        PlanParameter(name="duration_minutes", value=10, unit="kg"),
        PlanParameter(name="sets", value=3, unit="kg"),
    ],
)
def test_detailed_dose_requires_positive_number_and_sensible_unit(parameter):
    candidate = PlanCandidate(
        action="create",
        scope="week",
        start_date=date(2026, 10, 21),
        sessions=[
            PlannedSessionCandidate(
                date=date(2026, 10, 21),
                title="Аэробная база",
                purpose="Развивать спокойный объём",
                intensity="Памп не выше 3 из 10",
                rest_guidance="Восстановить дыхание между подходами",
                blocks=[
                    PlannedBlockCandidate(
                        kind="climb",
                        ad_hoc=True,
                        title="Спокойное лазание",
                        instruction="Лезь ровно и технически чисто.",
                        parameters=[parameter],
                    )
                ],
                stop_conditions=["Боль"],
                logging_guidance=["время и памп"],
            )
        ],
    )
    with pytest.raises(ValueError, match="needs dosage"):
        validate_detailed_plan(candidate, required=True)


@pytest.mark.parametrize(
    "parameter",
    [
        PlanParameter(name="duration_minutes", value=10, unit="min"),
        PlanParameter(name="duration_seconds", value=90, unit="sec"),
        PlanParameter(name="sets", value=3),
        PlanParameter(name="reps", value=8, unit="count"),
        PlanParameter(name="distance_m", value=1000, unit="m"),
    ],
)
def test_detailed_dose_accepts_supported_positive_quantities(parameter):
    candidate = PlanCandidate(
        action="create",
        scope="session",
        start_date=date(2026, 10, 21),
        title="Исполнимое занятие",
        purpose="Проверить дозировку",
        intensity="Легко",
        rest_guidance="По две минуты между подходами",
        blocks=[
            PlannedBlockCandidate(
                kind="climb",
                ad_hoc=True,
                title="Рабочий блок",
                instruction="Выполни назначенный объём.",
                parameters=[parameter],
            )
        ],
        stop_conditions=["Боль"],
        logging_guidance=["фактически выполненный объём"],
    )
    validate_detailed_plan(candidate, required=True)


def _active_program(athlete_id, *, start, end, location="скалодром", source_ids=None):
    return Plan(
        athlete_id=athlete_id,
        scope="program",
        status="active",
        visibility="private",
        start_date=start,
        end_date=end,
        content={"summary": "Программа", **({"location": location} if location else {})},
        evidence=[],
        source_message_ids=source_ids or [],
    )


async def test_first_week_task_binds_unique_current_program_with_venue(database):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        venue_source = await _message(
            session, athlete.id, 9330, "Буду тренироваться в скалодроме"
        )
        program = _active_program(
            athlete.id,
            start=NOW.date() - timedelta(days=1),
            end=NOW.date() + timedelta(days=20),
            source_ids=[venue_source.id],
        )
        session.add(program)
        await session.flush()
        root = await _message(
            session,
            athlete.id,
            9331,
            "Давай подробно спланируем первую неделю",
        )
        task = await sync_open_planning_task(
            session, preference=await session.get(TrainingPreference, athlete.id),
            message=root, now=NOW,
        )
        assert task and task.parent_plan_id == program.id
        assert task.program_binding_issue is None
        assert not task.ready  # the refinement goal is still intentionally missing

        answer = await _message(
            session,
            athlete.id,
            9332,
            "Спокойно войти",
            when=NOW + timedelta(minutes=1),
        )
        task = await sync_open_planning_task(
            session, preference=await session.get(TrainingPreference, athlete.id),
            message=answer, now=NOW + timedelta(minutes=1),
        )
        assert task and task.ready and task.parent_plan_id == program.id


@pytest.mark.parametrize("scenario", ["future", "venue_less", "overlap"])
async def test_first_week_task_does_not_bind_unsafe_program(database, scenario):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        venue_source = await _message(
            session, athlete.id, 9339, "Буду тренироваться в скалодроме"
        )
        if scenario == "future":
            programs = [
                _active_program(
                    athlete.id,
                    start=NOW.date() + timedelta(days=1),
                    end=NOW.date() + timedelta(days=30),
                    source_ids=[venue_source.id],
                )
            ]
        elif scenario == "venue_less":
            programs = [
                _active_program(
                    athlete.id,
                    start=NOW.date() - timedelta(days=1),
                    end=NOW.date() + timedelta(days=30),
                    location=None,
                    source_ids=[venue_source.id],
                )
            ]
        else:
            programs = [
                _active_program(
                    athlete.id,
                    start=NOW.date() - timedelta(days=offset),
                    end=NOW.date() + timedelta(days=30),
                    source_ids=[venue_source.id],
                )
                for offset in (1, 2)
            ]
        session.add_all(programs)
        await session.flush()
        root = await _message(
            session,
            athlete.id,
            9340,
            "Давай подробно спланируем первую неделю в скалодроме",
        )
        task = await sync_open_planning_task(
            session, preference=await session.get(TrainingPreference, athlete.id),
            message=root, now=NOW,
        )

        assert task and not task.ready
        assert task.parent_plan_id is None
        assert task.program_binding_issue in {
            "no_current_program_contains_week",
            "current_program_has_no_venue",
            "ambiguous_current_program",
        }


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


class FirstWeekDialogueAI(FakeAIService):
    def __init__(self):
        super().__init__(MessageExtraction(normalized_text=""))
        self.current_text = ""

    async def extract(self, *, text, **kwargs):
        self.current_text = text
        return MessageExtraction(
            normalized_text=text,
            should_respond=True,
            conversation_intent="continuation",
        )

    async def coach_reply(self, **kwargs):
        if "спокойно войти" not in self.current_text.casefold():
            return CoachReply(
                text="Какую цель ставим на первую неделю?",
                follow_up_question="Какую цель ставим на первую неделю?",
            )

        def planned(day, title, purpose, main_instruction):
            return PlannedSessionCandidate(
                date=day,
                title=title,
                purpose=purpose,
                location="скалодром",
                intensity="Легко: без отказа, памп не выше 3 из 10",
                rest_guidance="Между трассами отдыхай 2–3 минуты; перед следующим блоком восстановись полностью.",
                blocks=[
                    PlannedBlockCandidate(
                        kind="warmup",
                        ad_hoc=True,
                        title="Постепенная разминка",
                        instruction="Разогрейся и постепенно перейди к очень лёгкому лазанию.",
                        parameters=[PlanParameter(name="duration_minutes", value=15, unit="min")],
                    ),
                    PlannedBlockCandidate(
                        kind="climb",
                        ad_hoc=True,
                        title="Основной спокойный объём",
                        instruction=main_instruction,
                        parameters=[PlanParameter(name="duration_minutes", value=25, unit="min")],
                    ),
                    PlannedBlockCandidate(
                        kind="cooldown",
                        ad_hoc=True,
                        title="Заминка",
                        instruction="Заверши лёгкой ходьбой и мягкой мобилизацией.",
                        parameters=[PlanParameter(name="duration_minutes", value=8, unit="min")],
                    ),
                ],
                stop_conditions=["Боль или нарастающий дискомфорт", "Памп выше 4 из 10"],
                logging_guidance=["трассы и грейды", "максимальный памп", "самочувствие после"],
            )

        return CoachReply(
            text="Собрал спокойную первую неделю.",
            plan=PlanCandidate(
                action="create",
                scope="week",
                start_date=date(2026, 10, 21),
                end_date=date(2026, 10, 27),
                title="Спокойный вход в регулярные тренировки",
                summary="Вернуть регулярность и оценить переносимость объёма без отказа",
                sessions=[
                    planned(
                        date(2026, 10, 21),
                        "Лёгкий объём на автостраховке",
                        "Вернуть ровное трассовое лазание без выраженного пампа",
                        "Лезь простые трассы с полным контролем и отдыхай по самочувствию.",
                    ),
                    planned(
                        date(2026, 10, 23),
                        "Техника в боулдеринге",
                        "Вернуть точность ног и экономичность движений",
                        "Выбирай простые задачи и повторяй движения чисто, без силовых попыток.",
                    ),
                    planned(
                        date(2026, 10, 25),
                        "Смешанная восстановительная сессия",
                        "Закрепить спокойный недельный объём и проверить восстановление",
                        "Чередуй лёгкие трассы и технические боулдеры с полным восстановлением.",
                    ),
                ],
                guardrails=["Не повышать нагрузку при боли или ухудшении самочувствия."],
            ),
        )


async def test_first_week_refinement_keeps_month_program_and_renders_saved_sessions(
    database, settings
):
    current = datetime(2026, 10, 21, 14, tzinfo=UTC)
    ai = FirstWeekDialogueAI()
    processor = MessageProcessor(settings, ai)
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        source = await _message(
            session,
            athlete.id,
            9500,
            "Месячная программа развития выносливости в скалодроме",
            when=current - timedelta(minutes=2),
        )
        program = await save_plan(
            session,
            athlete_id=athlete.id,
            candidate=PlanCandidate(
                action="create",
                scope="program",
                start_date=date(2026, 10, 21),
                end_date=date(2026, 11, 20),
                summary="Подготовка выносливости к мультипитчу",
                location="скалодром",
            ),
            source_message_id=source.id,
            evidence=["месячная программа"],
            today=date(2026, 10, 21),
        )

        question = await processor.process(
            session,
            IncomingMessage(
                telegram_message_id=9501,
                telegram_chat_id=101,
                telegram_user_id=101,
                timestamp=current,
                text="давай подробнее спланируем первую неделю",
            ),
        )
        assert "Какую цель" in question.reply

        result = await processor.process(
            session,
            IncomingMessage(
                telegram_message_id=9502,
                telegram_chat_id=101,
                telegram_user_id=101,
                timestamp=current + timedelta(minutes=1),
                text="спокойно войти",
            ),
        )

        await session.refresh(program)
        assert program.status == "active"
        weeks = list(
            await session.scalars(
                select(Plan).where(
                    Plan.athlete_id == athlete.id,
                    Plan.scope == "week",
                    Plan.status == "active",
                )
            )
        )
        assert len(weeks) == 1 and weeks[0].parent_id == program.id
        children = list(
            await session.scalars(
                select(Plan).where(
                    Plan.parent_id == weeks[0].id,
                    Plan.scope == "session",
                    Plan.status == "active",
                )
            )
        )
        assert {child.start_date for child in children} == {
            date(2026, 10, 21),
            date(2026, 10, 23),
            date(2026, 10, 25),
        }
        assert all(len((child.content or {}).get("blocks", [])) == 3 for child in children)
        assert len((weeks[0].content or {}).get("sessions", [])) == 3
        assert "Занятия:" in result.reply
        assert "Лёгкий объём на автостраховке" in result.reply
        assert "После занятия сообщи:" in result.reply
        assert "памп не выше 3 из 10" in result.reply
