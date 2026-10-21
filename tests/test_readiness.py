from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import Athlete, Message
from app.readiness import recent_illness_unresolved

GROUP = -100123
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


async def _message(
    session,
    athlete_id: int,
    message_id: int,
    text: str,
    *,
    chat_id: int = GROUP,
    at: datetime = NOW,
    processing_data: dict | None = None,
) -> Message:
    message = Message(
        athlete_id=athlete_id,
        telegram_chat_id=chat_id,
        telegram_message_id=message_id,
        direction="incoming",
        raw_text=text,
        normalized_text=text,
        processing_data=processing_data or {},
        telegram_timestamp=at,
    )
    session.add(message)
    await session.flush()
    return message


async def test_recent_illness_is_cleared_only_by_explicit_full_recovery(database):
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        await _message(session, alexey.id, 8001, "Я приболел", at=NOW - timedelta(days=1))
        assert await recent_illness_unresolved(session, alexey.id, GROUP, NOW)

        await _message(session, alexey.id, 8002, "Андрей полностью выздоровел", at=NOW)
        assert await recent_illness_unresolved(session, alexey.id, GROUP, NOW)

        await _message(session, alexey.id, 8003, "Я не полностью выздоровел", at=NOW)
        assert await recent_illness_unresolved(session, alexey.id, GROUP, NOW)

        await _message(
            session,
            alexey.id,
            8004,
            "Когда я полностью выздоровел, вернусь к тренировкам.",
            at=NOW,
        )
        assert await recent_illness_unresolved(session, alexey.id, GROUP, NOW)

        await _message(session, alexey.id, 8005, "Я полностью выздоровел", at=NOW + timedelta(minutes=1))
        assert not await recent_illness_unresolved(
            session, alexey.id, GROUP, NOW + timedelta(minutes=2)
        )


async def test_readiness_uses_only_own_allowed_messages_in_current_chat(database):
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        andrey = await session.scalar(select(Athlete).where(Athlete.name == "Andrey"))
        assert alexey and andrey
        await _message(session, andrey.id, 8011, "Я заболел")
        await _message(session, alexey.id, 8012, "Я простудился", chat_id=101)
        await _message(
            session,
            alexey.id,
            8013,
            "Я болею",
            processing_data={"request_policy": {"decision": "other_athlete"}},
        )
        assert not await recent_illness_unresolved(session, alexey.id, GROUP, NOW)


async def test_readiness_ignores_stale_future_and_non_reports(database):
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        await _message(session, alexey.id, 8021, "Я заболел", at=NOW - timedelta(days=7, minutes=1))
        await _message(session, alexey.id, 8022, "Я приболел", at=NOW + timedelta(minutes=1))
        await _message(session, alexey.id, 8023, "Андрей заболел")
        await _message(session, alexey.id, 8024, "Если я заболею, что делать?")
        assert not await recent_illness_unresolved(session, alexey.id, GROUP, NOW)


async def test_readiness_accepts_first_person_fillers_and_prior_clause_before_question(database):
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        await _message(
            session,
            alexey.id,
            8031,
            "Привет! Давай спланируем дальше тренировки. Я правда чуть приболел на этой неделе, но, надеюсь, смогу полазать в воскресенье.",
        )
        await _message(session, alexey.id, 8032, "Я заболел. Как планировать?")
        assert await recent_illness_unresolved(session, alexey.id, GROUP, NOW)
