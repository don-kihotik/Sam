from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.ai import FakeAIService
from app.catalog import seed_catalog
from app.coaching import useful_followup, validate_catalog_choices
from app.db.models import CatalogProtocol, Plan, ProcessingRun, Workout
from app.schemas import (
    CoachReply,
    MessageExtraction,
    PlanCandidate,
    PlannedBlockCandidate,
    PlanParameter,
)
from app.services import IncomingMessage, MessageProcessor


def prescription(*, valid=True, day=date(2026, 9, 28), minutes=30):
    return CoachReply(text="Сегодня 90 минут. Не должно попасть в карточку.", plan=PlanCandidate(
        action="create", scope="session", start_date=day, end_date=day,
        summary="Спокойная техника ног", location="gym",
        blocks=[PlannedBlockCandidate(
            kind="technique", title="Тихие ноги", ad_hoc=valid,
            parameters=[PlanParameter(name="duration_minutes", value=minutes, unit="min")],
            instruction="На лёгких трассах ставь ногу один раз без перестановок; отдыхай 2 минуты между попытками.",
        )],
    ))


class DialogueAI(FakeAIService):
    def __init__(self, replies):
        super().__init__(MessageExtraction(normalized_text=""))
        self.replies = list(replies)
        self.calls = 0

    async def extract(self, *, text, **kwargs):
        return MessageExtraction(normalized_text=text, should_respond=True)

    async def coach_reply(self, *, context, **kwargs):
        self.coach_contexts.append(context)
        self.calls += 1
        return self.replies.pop(0).model_copy(deep=True)


def incoming(number, text):
    return IncomingMessage(
        telegram_message_id=number, telegram_chat_id=101, telegram_user_id=101,
        timestamp=datetime(2026,9,24,20,tzinfo=UTC) + timedelta(seconds=number - 9100), text=text,
    )


async def test_full_followup_uses_sunday_and_updates_same_session_family(database, settings):
    ai = DialogueAI([prescription(), prescription(minutes=20)])
    async with database.sessions() as session:
        before = await session.scalar(select(func.count()).select_from(Workout))
        processor = MessageProcessor(settings, ai)
        first = await processor.process(session, incoming(9100, "Составь занятие в воскресенье"))
        second = await processor.process(session, incoming(9101, "Какие упражнения посоветуешь?"))
        plans = list(await session.scalars(select(Plan).where(Plan.scope == "session").order_by(Plan.id)))
        assert len(plans) == 2
        assert plans[0].status == "superseded" and plans[1].status == "active"
        assert plans[1].supersedes_id == plans[0].id
        assert all(p.start_date == date(2026,9,27) for p in plans)
        assert "27" in first.reply and "27" in second.reply
        assert "90 минут" not in second.reply and "Сегодня" not in second.reply
        assert "duration_minutes" not in second.reply and "gym" not in second.reply
        assert await session.scalar(select(func.count()).select_from(Workout)) == before
        assert "SESSION INTENT" in ai.coach_contexts[-1]


async def test_bad_plan_gets_one_repair_and_auditable_reason(database, settings):
    ai = DialogueAI([prescription(valid=False), prescription()])
    async with database.sessions() as session:
        result = await MessageProcessor(settings, ai).process(session, incoming(9102, "План на воскресенье"))
        run = await session.scalar(select(ProcessingRun).where(ProcessingRun.message_id == result.message_id))
        diagnostics = run.extracted["coaching_diagnostics"]
        assert ai.calls == 2 and diagnostics[0]["status"] == "rejected"
        assert "ad_hoc" in diagnostics[0]["reason"]
        assert diagnostics[1]["status"] == "saved"
        assert "Не смог" not in result.reply
        assert await session.scalar(select(func.count()).select_from(Plan).where(Plan.scope == "session")) == 1


async def test_two_invalid_candidates_do_not_change_existing_plan(database, settings):
    ai = DialogueAI([prescription(), prescription(valid=False), prescription(valid=False)])
    async with database.sessions() as session:
        processor = MessageProcessor(settings, ai)
        await processor.process(session, incoming(9103, "План на воскресенье"))
        result = await processor.process(session, incoming(9104, "Можно короче?"))
        assert ai.calls == 3 and "Предыдущий план не изменён" in result.reply
        plans = list(await session.scalars(select(Plan).where(Plan.scope == "session")))
        assert len(plans) == 1 and plans[0].status == "active"


async def test_conflicting_dates_ask_human_question_without_model_plan(database, settings):
    ai = DialogueAI([])
    async with database.sessions() as session:
        result = await MessageProcessor(settings, ai).process(session, incoming(9105, "План на воскресенье 2026-09-28"))
        assert "Уточни дату" in result.reply and "conflicting_dates" not in result.reply
        assert ai.calls == 0


def test_health_question_asks_present_once_not_future():
    question = useful_followup("К воскресенью симптомы уйдут?", [])
    assert question == "Как ты себя чувствуешь сейчас — какие симптомы ещё остались?"
    assert useful_followup("Есть сейчас слабость?", [question]) is None
    assert useful_followup("Какой вес был в жиме?", [question]) == "Какой вес был в жиме?"


def test_noop_plan_discards_irrelevant_mixed_containers():
    candidate = PlanCandidate.model_validate({"action": "none", "scope": "session",
        "blocks": [{"kind": "technique", "ad_hoc": True}],
        "sessions": [{"title": "Unused", "blocks": [{"kind": "warmup"}]}]})
    assert candidate.action == "none" and not candidate.blocks and not candidate.sessions


async def test_extractor_and_coach_do_not_both_update_the_same_target(database, settings):
    class ExtractingAI(DialogueAI):
        async def extract(self, *, text, **kwargs):
            return MessageExtraction(normalized_text=text, should_respond=True,
                plan=prescription().plan.model_copy(update={"evidence": [text]}))
    ai = ExtractingAI([prescription(), prescription(minutes=20)])
    async with database.sessions() as session:
        processor = MessageProcessor(settings, ai)
        await processor.process(session, incoming(9106, "План на воскресенье"))
        result = await processor.process(session, incoming(9107, "Сократи до 20 минут"))
        plans = list(await session.scalars(select(Plan).where(Plan.scope == "session")))
        assert len(plans) == 2 and sum(p.status == "active" for p in plans) == 1
        assert "Не смог" not in result.reply


async def test_catalog_illness_and_impersonation_guards(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        drop = await session.scalar(select(CatalogProtocol).where(CatalogProtocol.slug == "drop-intervals"))
        candidate = prescription().plan
        candidate.blocks = [PlannedBlockCandidate(kind="climbing", title=drop.title,
            protocol_id=drop.id, protocol_version=drop.version)]
        with pytest.raises(ValueError, match="illness"):
            await validate_catalog_choices(session, candidate, illness_unresolved=True)
        await validate_catalog_choices(session, candidate, illness_unresolved=False)
        candidate.blocks[0].protocol_id = None
        candidate.blocks[0].ad_hoc = True
        with pytest.raises(ValueError, match="descriptive title"):
            await validate_catalog_choices(session, candidate, illness_unresolved=False)


async def test_requested_duration_is_validated_and_survives_reschedule(database, settings):
    ai = DialogueAI([prescription(minutes=45), prescription(minutes=45), prescription(minutes=30),
                    prescription(minutes=45), prescription(minutes=30)])
    async with database.sessions() as session:
        processor = MessageProcessor(settings, ai)
        await processor.process(session, incoming(9108, "План на воскресенье"))
        short = await processor.process(session, incoming(9109, "Сократи занятие до 30 минут"))
        assert "около 30 мин" in short.reply
        moved = await processor.process(session, incoming(9110, "Перенеси на пятницу 2026-10-02"))
        assert "около 30 мин" in moved.reply and ai.calls == 5
        plans = list(await session.scalars(select(Plan).where(Plan.scope == "session", Plan.status == "active")))
        assert len(plans) == 1 and plans[0].content["max_duration_minutes"] == 30
        assert plans[0].start_date == date(2026, 10, 2)
