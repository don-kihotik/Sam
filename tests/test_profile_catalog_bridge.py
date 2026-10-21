from datetime import UTC, date, datetime

from app.catalog import seed_catalog
from app.db.models import Fact, Message, TrainingPreference
from app.memory import catalog_options
from app.training_profile import effective_training_profile


async def _gym_access_fact(
    session, *, text: str, visibility: str = "private", source_chat_id: int | None = None
) -> None:
    source_chat_id = source_chat_id if source_chat_id is not None else (101 if visibility == "private" else -100123)
    message = Message(
        telegram_message_id=88001 if visibility == "private" else 88002,
        telegram_chat_id=source_chat_id,
        telegram_user_id=202,
        athlete_id=2,
        direction="incoming",
        message_type="text",
        raw_text=text,
        normalized_text=text,
        telegram_timestamp=datetime(2026, 9, 24, tzinfo=UTC),
    )
    session.add(message)
    await session.flush()
    session.add(
        Fact(
            athlete_id=2,
            field="gym_access",
            value=text,
            source_message_id=message.id,
            visibility=visibility,
            status="active",
        )
    )
    await session.flush()


async def test_dated_gym_access_is_ephemeral_but_enables_current_catalog_request(database):
    text = "В воскресенье могу и болдеринг и с автостраховкой полазать"
    sunday = date(2026, 9, 27)
    async with database.sessions() as session:
        await _gym_access_fact(session, text=text)
        await seed_catalog(session)

        sunday_profile = await effective_training_profile(
            session, athlete_id=2, session_date=sunday
        )
        equipment = {item["name"] for item in sunday_profile["equipment"]}
        assert {"bouldering wall", "route or circuit wall", "auto_belay"} <= equipment
        assert "lead rope" not in equipment

        choices = await catalog_options(
            session,
            athlete_id=2,
            session_date=sunday,
            session_text=text,
            reference_date=date(2026, 9, 24),
        )
        titles = {
            protocol["title"] for choice in choices for protocol in choice["protocols"]
        }
        assert "Warm Up Protocol - Bouldering" in titles

        monday_profile = await effective_training_profile(
            session, athlete_id=2, session_date=date(2026, 9, 28)
        )
        assert not monday_profile.get("equipment")
        next_sunday = await effective_training_profile(
            session, athlete_id=2, session_date=date(2026, 10, 4)
        )
        assert not next_sunday.get("equipment")
        # A dated request does not leak into a different day's selection.
        transient = await effective_training_profile(
            session,
            athlete_id=2,
            session_date=date(2026, 9, 28),
            session_text=text,
            reference_date=date(2026, 9, 24),
        )
        assert not transient.get("equipment")
        # An undated current request is a one-off override, not a preference write.
        ephemeral = await effective_training_profile(
            session,
            athlete_id=2,
            session_date=date(2026, 9, 28),
            session_text="Могу болдеринг и автостраховку",
            reference_date=date(2026, 9, 24),
        )
        assert {item["name"] for item in ephemeral["equipment"]} >= {"bouldering wall", "auto_belay"}
        preference = await session.get(TrainingPreference, 2)
        assert preference is not None and preference.profile == {}


async def test_effective_catalog_profile_respects_visibility_and_explicit_unavailability(database):
    text = "В воскресенье могу и болдеринг и с автостраховкой полазать"
    sunday = date(2026, 9, 27)
    async with database.sessions() as session:
        await _gym_access_fact(session, text=text, visibility="private")
        private_fact_in_group = await effective_training_profile(
            session, athlete_id=2, public=True, session_date=sunday
        )
        assert not private_fact_in_group.get("equipment")

        preference = await session.get(TrainingPreference, 2)
        assert preference is not None
        preference.profile = {
            "equipment": [
                {"name": "bouldering wall", "available": False},
                {"name": "route or circuit wall", "available": False},
                {"name": "auto_belay", "available": False},
            ]
        }
        effective = await effective_training_profile(
            session, athlete_id=2, session_date=sunday
        )
        unavailable = {
            item["name"]
            for item in effective["equipment"]
            if item.get("available") is False
        }
        assert unavailable == {"bouldering wall", "route or circuit wall", "auto_belay"}
        assert not any(item.get("available") is True for item in effective["equipment"])


async def test_access_parser_handles_per_equipment_negation_and_private_source(database):
    sunday = date(2026, 9, 27)
    async with database.sessions() as session:
        # A fact labelled group-visible is still private if its evidence message
        # originated in a private chat; it must not enter a group catalog context.
        await _gym_access_fact(
            session,
            text="В воскресенье могу болдеринг",
            visibility="group",
            source_chat_id=101,
        )
        public = await effective_training_profile(
            session, athlete_id=2, public=True, session_date=sunday
        )
        assert not public.get("equipment")

        preference = await session.get(TrainingPreference, 2)
        assert preference is not None
        preference.profile = {
            "equipment": [
                {"name": "bouldering wall", "available": True, "location": "climbing_gym"},
                {"name": "route or circuit wall", "available": True, "location": "climbing_gym"},
                {"name": "auto_belay", "available": True, "location": "climbing_gym"},
            ]
        }
        negative = await effective_training_profile(
            session,
            athlete_id=2,
            session_date=sunday,
            session_text="Нет автостраховки, только болдеринг",
            reference_date=date(2026, 9, 24),
        )
        assert {item["name"] for item in negative["equipment"] if item.get("available")} == {
            "bouldering wall"
        }
        assert {
            item["name"] for item in negative["equipment"] if item.get("available") is False
        } >= {"route or circuit wall", "auto_belay"}
        unrelated = await effective_training_profile(
            session,
            athlete_id=999,
            session_date=date(2026, 9, 24),
            session_text="Сегодня бег вместо болдеринга",
            reference_date=date(2026, 9, 24),
        )
        assert not unrelated.get("equipment")


async def test_access_dates_share_session_intent_grammar(database):
    async with database.sessions() as session:
        exact_next_sunday = await effective_training_profile(
            session,
            athlete_id=999,
            session_date=date(2026, 10, 4),
            session_text="В следующее воскресенье, 2026-10-04 могу болдеринг",
            reference_date=date(2026, 9, 24),
        )
        assert {item["name"] for item in exact_next_sunday["equipment"]} == {"bouldering wall"}
        # "средний" must not be mistaken for Wednesday.
        ordinary = await effective_training_profile(
            session,
            athlete_id=999,
            session_date=date(2026, 9, 25),
            session_text="Средний темп; могу болдеринг",
            reference_date=date(2026, 9, 24),
        )
        assert {item["name"] for item in ordinary["equipment"]} == {"bouldering wall"}
