from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app.db.models import Athlete, Message, Plan
from app.session_intent import resolve_session_intent

CHAT = -100123
PRIVATE_CHAT = 101
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)  # Thursday


async def _message(
    session,
    athlete_id: int,
    telegram_id: int,
    text: str,
    *,
    chat_id: int = CHAT,
    when: datetime = NOW,
    allowed: bool = True,
) -> Message:
    message = Message(
        athlete_id=athlete_id,
        telegram_chat_id=chat_id,
        telegram_message_id=telegram_id,
        direction="incoming",
        raw_text=text,
        normalized_text=text,
        processing_data={"request_policy": {"decision": "allow" if allowed else "other_athlete"}},
        telegram_timestamp=when,
    )
    session.add(message)
    await session.flush()
    return message


async def _session_plan(
    session,
    athlete_id: int,
    source_id: int,
    day: date,
    *,
    visibility: str = "private",
) -> Plan:
    plan = Plan(
        athlete_id=athlete_id,
        scope="session",
        status="active",
        visibility=visibility,
        start_date=day,
        end_date=day,
        content={},
        source_message_ids=[source_id],
    )
    session.add(plan)
    await session.flush()
    return plan


async def test_weekdays_today_tomorrow_iso_and_next_qualifier_are_deterministic(database):
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        sunday = await resolve_session_intent(
            session, athlete_id=alexey.id, chat_id=CHAT, text="в воскресенье", today=NOW.date(), now=NOW
        )
        english = await resolve_session_intent(
            session, athlete_id=alexey.id, chat_id=CHAT, text="Tuesday", today=NOW.date(), now=NOW
        )
        iso = await resolve_session_intent(
            session, athlete_id=alexey.id, chat_id=CHAT, text="2026-10-03", today=NOW.date(), now=NOW
        )
        next_friday = await resolve_session_intent(
            session, athlete_id=alexey.id, chat_id=CHAT, text="next Friday", today=NOW.date(), now=NOW
        )
        dated_friday = await resolve_session_intent(
            session,
            athlete_id=alexey.id,
            chat_id=CHAT,
            text="Перенеси на пятницу 2026-10-02",
            today=NOW.date(),
            now=NOW,
        )
        mismatched_dated_weekday = await resolve_session_intent(
            session,
            athlete_id=alexey.id,
            chat_id=CHAT,
            text="воскресенье 2026-09-28",
            today=NOW.date(),
            now=NOW,
        )
        middle = await resolve_session_intent(
            session, athlete_id=alexey.id, chat_id=CHAT, text="средний хват", today=NOW.date(), now=NOW
        )
    assert sunday.date == date(2026, 9, 27)
    assert english.date == date(2026, 9, 29)
    assert iso.date == date(2026, 10, 3)
    assert next_friday.date == date(2026, 10, 2)
    assert dated_friday.date == date(2026, 10, 2)
    assert mismatched_dated_weekday.ambiguity == "conflicting_dates"
    assert middle.date is None


async def test_date_conflicts_foreign_ids_and_natural_duration_do_not_select_plan(database):
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        andrey = await session.scalar(select(Athlete).where(Athlete.name == "Andrey"))
        assert alexey and andrey
        foreign_message = await _message(session, andrey.id, 1001, "в воскресенье")
        foreign_plan = await _session_plan(session, andrey.id, foreign_message.id, date(2026, 9, 27))
        own_message = await _message(session, alexey.id, 1002, "в пятницу")
        own_plan = await _session_plan(
            session, alexey.id, own_message.id, date(2026, 9, 25), visibility="group"
        )
        conflict = await resolve_session_intent(
            session, athlete_id=alexey.id, chat_id=CHAT, text="сегодня или завтра", today=NOW.date(), now=NOW
        )
        foreign = await resolve_session_intent(
            session, athlete_id=alexey.id, chat_id=CHAT, text=f"/session #{foreign_plan.id}", today=NOW.date(), now=NOW
        )
        duration = await resolve_session_intent(
            session, athlete_id=alexey.id, chat_id=CHAT, text="тренировка 30 минут", today=NOW.date(), now=NOW
        )
        explicit = await resolve_session_intent(
            session, athlete_id=alexey.id, chat_id=CHAT, text=f"план #{own_plan.id}", today=NOW.date(), now=NOW
        )
    assert conflict.ambiguity == "conflicting_dates"
    assert foreign.ambiguity == "unknown_or_inactive_session"
    assert duration.target_plan_id is None
    assert explicit.target_plan_id == own_plan.id


async def test_group_never_resolves_private_session_even_for_owner(database):
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        private_source = await _message(session, alexey.id, 1003, "на воскресенье", chat_id=PRIVATE_CHAT)
        private_plan = await _session_plan(session, alexey.id, private_source.id, date(2026, 9, 27))
        private_id = await resolve_session_intent(
            session, athlete_id=alexey.id, chat_id=CHAT, text=f"план #{private_plan.id}", today=NOW.date(), now=NOW
        )
        private_followup = await resolve_session_intent(
            session, athlete_id=alexey.id, chat_id=CHAT, text="какие упражнения?", today=NOW.date(), now=NOW
        )
    assert private_id.ambiguity == "unknown_or_inactive_session"
    assert private_followup.target_plan_id is None


async def test_refinement_reschedules_only_recent_source_linked_group_session(database):
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        source = await _message(session, alexey.id, 1004, "составь на воскресенье")
        plan = await _session_plan(session, alexey.id, source.id, date(2026, 9, 27), visibility="group")
        rescheduled = await resolve_session_intent(
            session,
            athlete_id=alexey.id,
            chat_id=CHAT,
            text="перенеси на пятницу",
            today=NOW.date(),
            now=NOW,
        )
        shortened = await resolve_session_intent(
            session, athlete_id=alexey.id, chat_id=CHAT, text="сократи до 30 минут", today=NOW.date(), now=NOW
        )
        new_plan = await resolve_session_intent(
            session, athlete_id=alexey.id, chat_id=CHAT, text="следующая тренировка в пятницу", today=NOW.date(), now=NOW
        )
    assert rescheduled.target_plan_id == plan.id and rescheduled.date == date(2026, 9, 25)
    assert shortened.target_plan_id == plan.id
    assert new_plan.target_plan_id is None and new_plan.date == date(2026, 9, 25)


async def test_followup_uses_latest_allowed_planning_date_not_old_or_reported_date(database):
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        old = await _message(session, alexey.id, 1005, "на воскресенье", when=NOW - timedelta(hours=3))
        await _session_plan(session, alexey.id, old.id, date(2026, 9, 27), visibility="group")
        await _message(session, alexey.id, 1006, "в понедельник пробежал 5 км", when=NOW - timedelta(hours=2))
        latest = await _message(session, alexey.id, 1007, "на вторник хочу тренировку", when=NOW - timedelta(hours=1))
        await _message(session, alexey.id, 1008, "на пятницу", when=NOW - timedelta(minutes=30), allowed=False)
        future = await _message(session, alexey.id, 1009, "на среду", when=NOW + timedelta(minutes=1))
        intent = await resolve_session_intent(
            session,
            athlete_id=alexey.id,
            chat_id=CHAT,
            text="могу комбинировать?",
            today=NOW.date(),
            now=NOW,
        )
    assert latest.id != future.id
    assert intent.target_plan_id is None
    assert intent.date == date(2026, 9, 29)


async def test_followup_anchors_relative_dates_to_source_local_day_and_repairs_bad_linked_day(database):
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        # Sep 25 UTC is still Sep 24 in Alexey's America/Toronto profile.
        first_now = datetime(2026, 9, 25, 2, tzinfo=UTC)
        followup_now = datetime(2026, 9, 25, 16, tzinfo=UTC)
        tomorrow_source = await _message(
            session,
            alexey.id,
            1010,
            "завтра хочу тренировку",
            when=first_now,
        )
        carried = await resolve_session_intent(
            session,
            athlete_id=alexey.id,
            chat_id=CHAT,
            text="могу комбинировать?",
            today=followup_now.date(),
            now=followup_now,
        )
        sunday_source = await _message(
            session,
            alexey.id,
            1011,
            "составь на воскресенье",
            when=followup_now - timedelta(minutes=1),
        )
        wrong_day_plan = await _session_plan(
            session,
            alexey.id,
            sunday_source.id,
            date(2026, 9, 28),
            visibility="group",
        )
        repaired = await resolve_session_intent(
            session,
            athlete_id=alexey.id,
            chat_id=CHAT,
            text="сократи до 20 минут",
            today=followup_now.date(),
            now=followup_now,
        )
    assert tomorrow_source.id
    assert carried.date == date(2026, 9, 25)
    assert carried.target_plan_id is None
    assert repaired.date == date(2026, 9, 27)
    assert repaired.target_plan_id == wrong_day_plan.id


async def test_same_day_conversation_refinements_reuse_the_current_chat_session(database):
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        source = await _message(session, alexey.id, 1012, "Приболел, но хочу полазать в воскресенье")
        sunday_plan = await _session_plan(
            session,
            alexey.id,
            source.id,
            date(2026, 9, 27),
            visibility="group",
        )
        advice = await resolve_session_intent(
            session,
            athlete_id=alexey.id,
            chat_id=CHAT,
            text="Что посоветуешь как тренировку в воскресенье?",
            today=NOW.date(),
            now=NOW,
        )
        availability = await resolve_session_intent(
            session,
            athlete_id=alexey.id,
            chat_id=CHAT,
            text="В воскресенье могу и болдеринг, и с автостраховкой полазать",
            today=NOW.date(),
            now=NOW,
        )
    assert advice.date == date(2026, 9, 27)
    assert advice.target_plan_id == sunday_plan.id
    assert availability.date == date(2026, 9, 27)
    assert availability.target_plan_id == sunday_plan.id
