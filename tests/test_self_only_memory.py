from datetime import UTC, date, datetime

from sqlalchemy import select

from app.db.models import Athlete, BacklogItem, Message, Workout
from app.memory import ContextBuilder, recent_message_text
from app.prompts import PROMPT_VERSION

GROUP = -100123


async def _message(
    session,
    athlete_id: int,
    telegram_id: int,
    text: str,
    *,
    direction: str = "incoming",
    chat_id: int = GROUP,
    processing_data: dict | None = None,
) -> Message:
    message = Message(
        athlete_id=athlete_id,
        telegram_chat_id=chat_id,
        telegram_message_id=telegram_id,
        direction=direction,
        raw_text=text,
        normalized_text=text,
        processing_data=processing_data or {},
        telegram_timestamp=datetime(2026, 9, 24, 12, tzinfo=UTC),
    )
    session.add(message)
    await session.flush()
    return message


async def test_recent_group_history_is_actor_incoming_only(database):
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        andrey = await session.scalar(select(Athlete).where(Athlete.name == "Andrey"))
        assert alexey and andrey
        await _message(session, alexey.id, 901, "OWN_INCOMING_CANARY")
        await _message(session, andrey.id, 902, "FOREIGN_INCOMING_CANARY")
        await _message(
            session,
            alexey.id,
            911,
            "DENIED_INCOMING_CANARY",
            processing_data={"request_policy": {"decision": "other_athlete"}},
        )
        await _message(
            session, alexey.id, 903, "LEGACY_OUTGOING_FOREIGN_CANARY", direction="outgoing"
        )
        await _message(
            session,
            alexey.id,
            908,
            "OWN_CURRENT_COACHING_CANARY",
            direction="outgoing",
            processing_data={"response_policy": PROMPT_VERSION},
        )
        await _message(
            session,
            andrey.id,
            909,
            "FOREIGN_CURRENT_COACHING_CANARY",
            direction="outgoing",
            processing_data={"response_policy": PROMPT_VERSION},
        )
        recent = await recent_message_text(session, alexey.id, chat_id=GROUP)
        await _message(session, alexey.id, 906, "OWN_PRIVATE_CANARY", chat_id=101)
        await _message(session, andrey.id, 907, "FOREIGN_PRIVATE_CANARY", chat_id=101)
        private_recent = await recent_message_text(session, alexey.id, chat_id=101)
    assert "OWN_INCOMING_CANARY" in recent
    assert "FOREIGN_INCOMING_CANARY" not in recent
    assert "DENIED_INCOMING_CANARY" not in recent
    assert "LEGACY_OUTGOING_FOREIGN_CANARY" not in recent
    assert "OWN_CURRENT_COACHING_CANARY" in recent
    assert "FOREIGN_CURRENT_COACHING_CANARY" not in recent
    assert "OWN_PRIVATE_CANARY" in private_recent
    assert "FOREIGN_PRIVATE_CANARY" not in private_recent


async def test_reply_parent_is_owned_incoming_and_context_has_no_peer_or_backlog(database):
    today = date(2026, 9, 24)
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        andrey = await session.scalar(select(Athlete).where(Athlete.name == "Andrey"))
        assert alexey and andrey
        own = await _message(session, alexey.id, 904, "OWN_REPLY_CANARY")
        foreign = await _message(session, andrey.id, 905, "FOREIGN_REPLY_CANARY")
        denied = await _message(
            session,
            alexey.id,
            912,
            "DENIED_REPLY_CANARY",
            processing_data={"request_policy": {"decision": "offtopic"}},
        )
        own_coach = await _message(
            session,
            alexey.id,
            910,
            "OWN_COACH_REPLY_CANARY",
            direction="outgoing",
            processing_data={"response_policy": PROMPT_VERSION},
        )
        session.add(Workout(athlete_id=andrey.id, date=today, type="climbing", status="active"))
        session.add(
            BacklogItem(
                created_by=andrey.id,
                type="idea",
                title="FOREIGN_BACKLOG_CANARY",
                original_message_id=foreign.id,
                notes={"visibility": "group"},
            )
        )
        session.add(
            BacklogItem(
                created_by=alexey.id,
                type="idea",
                title="OWN_BACKLOG_CANARY",
                original_message_id=own.id,
            )
        )
        await session.flush()
        builder = ContextBuilder(group_chat_id=GROUP)
        foreign_reply = await builder.extraction_context(
            session,
            athlete=alexey,
            chat_id=GROUP,
            reply_to_message_id=foreign.telegram_message_id,
            followup=None,
        )
        own_reply = await builder.extraction_context(
            session,
            athlete=alexey,
            chat_id=GROUP,
            reply_to_message_id=own.telegram_message_id,
            followup=None,
        )
        denied_reply = await builder.extraction_context(
            session,
            athlete=alexey,
            chat_id=GROUP,
            reply_to_message_id=denied.telegram_message_id,
            followup=None,
        )
        coach_reply = await builder.extraction_context(
            session,
            athlete=alexey,
            chat_id=GROUP,
            reply_to_message_id=own_coach.telegram_message_id,
            followup=None,
        )
        context = await builder.build(
            session,
            athlete=alexey,
            today=today,
            current_message="my question",
            semantic_matches=[],
            extraction_summary="{}",
            chat_id=GROUP,
        )
    assert "FOREIGN_REPLY_CANARY" not in foreign_reply
    assert "DENIED_REPLY_CANARY" not in denied_reply
    assert "OWN_REPLY_CANARY" in own_reply
    assert "OWN_COACH_REPLY_CANARY" in coach_reply
    assert "FOREIGN_BACKLOG_CANARY" not in context
    assert "OWN_BACKLOG_CANARY" in context
    assert "SHARED TRAINING OVERVIEW" not in context
    assert "Andrey" not in context
