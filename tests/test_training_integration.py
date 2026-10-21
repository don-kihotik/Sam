from datetime import UTC, date, datetime

from sqlalchemy import func, select

from app.ai import FakeAIService
from app.catalog import seed_catalog
from app.db.models import (
    Athlete,
    CatalogProtocol,
    Event,
    Message,
    Outbox,
    Plan,
    TrainingPreference,
    Workout,
)
from app.memory import ContextBuilder, catalog_options
from app.planning import _week_day
from app.records import stated_measurement
from app.schemas import (
    EquipmentCandidate,
    EventCandidate,
    MessageExtraction,
    TrainingProfileCandidate,
)
from app.seed import seed_initial_data
from app.services import IncomingMessage, MessageProcessor


def incoming(number, text, user=101, chat=101):
    return IncomingMessage(
        telegram_message_id=number,
        telegram_chat_id=chat,
        telegram_user_id=user,
        timestamp=datetime(2026, 9, 24, 12, tzinfo=UTC),
        text=text,
    )


def test_spoken_measurement_units_are_converted_without_guessing():
    assert stated_measurement("40 килограммов.", 40, "kg")
    assert stated_measurement("держал 1 минуту.", 60, "seconds")
    assert stated_measurement("1.5 часа", 90, "minutes")
    assert not stated_measurement("40 повторов", 40, "kg")
    assert not stated_measurement("40 килограммов", 40, "minutes")


def test_common_weekday_abbreviations_materialize_on_the_same_day():
    monday = date(2026, 9, 21)
    for label in ("Пн", "mon", "Monday", "понедельник"):
        assert _week_day(monday, label) == monday
    assert _week_day(monday, "Чт") == date(2026, 9, 24)


async def test_private_command_is_durable_and_duplicate_safe(database, settings):
    class NoModel(FakeAIService):
        async def extract(self, **kwargs):
            raise AssertionError("Settings commands must not use the model")

    processor = MessageProcessor(settings, NoModel(MessageExtraction(normalized_text="")))
    async with database.sessions() as s:
        first = await processor.process(s, incoming(70001, "/morning 08:15"))
        second = await processor.process(s, incoming(70001, "/morning 08:15"))
        preference = await s.get(TrainingPreference, 1)
        outbox_count = await s.scalar(select(func.count()).select_from(Outbox))
    assert "08:15" in first.reply
    assert second.duplicate
    assert preference.morning_time == "08:15"
    assert outbox_count == 1


async def test_third_athlete_is_not_shared_or_given_shared_trip(database, settings):
    async with database.sessions() as s:
        third = Athlete(name="Private Friend", telegram_user_id=303, profile={}, active=True)
        s.add(third)
        await s.flush()
        s.add(TrainingPreference(athlete_id=third.id, profile={}))
        s.add(
            Workout(
                athlete_id=third.id,
                date=date(2026, 9, 24),
                type="run",
                duration_minutes=987,
                entries=[],
            )
        )
        await s.commit()
        athlete = await s.get(Athlete, 1)
        context = await ContextBuilder(group_chat_id=settings.telegram_allowed_chat_id).build(
            s,
            athlete=athlete,
            today=date(2026, 9, 24),
            current_message="обзор",
            semantic_matches=[],
            extraction_summary="{}",
            chat_id=settings.telegram_allowed_chat_id,
        )
        assert "Private Friend" not in context
        assert "987" not in context
        text = "Сэм, общую поездку перенесли на декабрь"
        extraction = MessageExtraction(
            normalized_text=text,
            event=EventCandidate(
                action="update",
                shared=True,
                name="Trip",
                date_label="December",
                evidence=[text],
            ),
        )
        await MessageProcessor(settings, FakeAIService(extraction)).process(
            s, incoming(70002, text, chat=settings.telegram_allowed_chat_id)
        )
        third_events = await s.scalar(
            select(func.count()).select_from(Event).where(Event.athlete_id == third.id)
        )
        assert third_events == 0
        result = await MessageProcessor(settings, FakeAIService(extraction)).process(
            s, incoming(70003, text, user=303, chat=settings.telegram_allowed_chat_id)
        )
        assert result.ignored_unknown_user


async def test_profile_is_source_gated_and_not_in_group_context(database, settings):
    text = "Дома есть турник, PRIVATE_LOCATION"
    extraction = MessageExtraction(
        normalized_text=text,
        training_profile=TrainingProfileCandidate(
            equipment=[
                EquipmentCandidate(
                    name="pull-up bar", available=True, location="PRIVATE_LOCATION", evidence=[text]
                ),
                EquipmentCandidate(name="barbell", available=True, evidence=["not actually said"]),
            ]
        ),
    )
    async with database.sessions() as s:
        await MessageProcessor(settings, FakeAIService(extraction)).process(
            s, incoming(70004, text)
        )
        preference = await s.get(TrainingPreference, 1)
        assert len(preference.profile["equipment"]) == 1
        assert preference.profile["equipment"][0]["name"] == "pull-up bar"
        athlete = await s.get(Athlete, 1)
        context = await ContextBuilder(group_chat_id=settings.telegram_allowed_chat_id).build(
            s,
            athlete=athlete,
            today=date(2026, 9, 24),
            current_message="обзор",
            semantic_matches=[],
            extraction_summary="{}",
            chat_id=settings.telegram_allowed_chat_id,
        )
        assert "PRIVATE_LOCATION" not in context


async def test_cross_user_private_chat_rejected_even_without_runtime(database, settings):
    async with database.sessions() as s:
        before = await s.scalar(select(func.count()).select_from(Message))
        result = await MessageProcessor(
            settings, FakeAIService(MessageExtraction(normalized_text="/today"))
        ).process(s, incoming(70005, "/today", user=101, chat=202))
        after = await s.scalar(select(func.count()).select_from(Message))
        assert result.ignored_unknown_user
        assert before == after


async def test_catalog_context_keeps_venue_equipment_separate(database):
    async with database.sessions() as s:
        for slug, gear in (
            ("mat", ["exercise mat"]),
            ("bar", ["pull-up bar"]),
            ("both", ["exercise mat", "pull-up bar"]),
        ):
            s.add(
                CatalogProtocol(
                    slug=slug,
                    version=1,
                    title=slug,
                    category="conditioning",
                    status="ready",
                    specification={"equipment": {"required_all": gear}},
                )
            )
        await s.flush()
        choices = await catalog_options(
            s,
            {
                "equipment": [
                    {"name": "exercise mat", "available": True, "location": "home"},
                    {"name": "pull-up bar", "available": True, "location": "gym"},
                ],
            },
        )
        by_location = {
            item["only_at_location"]: {p["slug"] for p in item["protocols"]} for item in choices
        }
        assert by_location == {"home": {"mat"}, "gym": {"bar"}}


async def test_catalog_options_exposes_ranked_nonselectable_grade_question(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        choices = await catalog_options(
            session,
            {
                "equipment": ["auto_belay"],
                "locations": ["climbing_gym"],
                "goals": ["Выносливость для мультипитча"],
            },
            session_text="Подбери тренировку на выносливость",
        )
        assert len(choices) == 1
        choice = choices[0]
        assert choice["protocols"] == []
        blocked = choice["blocked_candidates"]
        assert blocked
        assert blocked[0]["selectable"] is False
        assert blocked[0]["missing_profile"] == ["flash_grade"]
        assert "protocol_id" not in blocked[0]
        assert choice["missing_profile_hints"][0] == {
            "field": "flash_grade",
            "reason": "required_by_relevant_ready_protocols",
            "blocked_candidate_count": 4,
            "top_candidate_slugs": ["1-on-1-off", "1-on-2-off", "aero-4s"],
        }


async def test_context_catalog_ranking_prefers_today_focus_over_durable_goal(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        athlete = await session.get(Athlete, 1)
        preference = await session.get(TrainingPreference, athlete.id)
        preference.profile = {
            "goals": [
                {
                    "title": "Развивать аэробную выносливость",
                    "visibility": "private",
                }
            ],
            "locations": [
                {"name": "climbing_gym", "visibility": "private"}
            ],
            "equipment": [
                {
                    "name": "bouldering wall",
                    "available": True,
                    "location": "climbing_gym",
                    "visibility": "private",
                }
            ],
            "experience": [
                {
                    "activity": "bouldering",
                    "flash_grade": "V3",
                    "visibility": "private",
                }
            ],
        }
        context = await ContextBuilder(group_chat_id=-100123).build(
            session,
            athlete=athlete,
            today=date(2026, 9, 24),
            current_message="Сегодня хочу занятие на технику ног",
            semantic_matches=[],
            extraction_summary="{}",
            chat_id=athlete.telegram_user_id,
        )
        catalog_section = context.split(
            "ELIGIBLE CATALOG PROTOCOLS (not completed workouts)", 1
        )[1].split("DETERMINISTIC ANALYTICS", 1)[0]
        assert catalog_section.index("Efficiency Training") < catalog_section.index(
            "Drop Intervals"
        )


async def test_legacy_seed_scope_is_classified_without_resetting_preferences(database, settings):
    async with database.sessions() as s:
        original = await s.scalar(select(Plan).where(Plan.athlete_id == 1))
        original.scope = "week"  # what the additive migration gives an existing baseline
        pref = await s.get(TrainingPreference, 1)
        pref.shared_chat_id = None
        pref.morning_time = "08:15"
        await s.commit()
        await seed_initial_data(s, settings)
        assert original.scope == "program"
        assert pref.shared_chat_id is None and pref.morning_time == "08:15"
