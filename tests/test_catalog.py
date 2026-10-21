from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.catalog import (
    _concepts,
    catalog_context,
    catalog_selection,
    receive_catalog_source,
    seed_catalog,
    validate_blocks,
)
from app.db.models import CatalogProtocol, CatalogSource, Message, Workout


async def _protocol(session, slug: str) -> CatalogProtocol:
    row = await session.scalar(select(CatalogProtocol).where(CatalogProtocol.slug == slug))
    assert row is not None
    return row


async def test_seed_catalog_is_idempotent_and_keeps_all_inventory(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        first = await session.scalar(select(func.count()).select_from(CatalogProtocol))
        await seed_catalog(session)
        second = await session.scalar(select(func.count()).select_from(CatalogProtocol))
        assert first == second == 70


async def test_conflicting_and_percent_load_cards_are_not_published(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        conflicting = await _protocol(session, "basic-grip-engagement")
        max_hangs = await _protocol(session, "max-hangs-90")
        pinch = await _protocol(session, "pinch-block-max-80")
        assert conflicting.status == "needs_review"
        assert conflicting.specification["source_completeness"] == "conflicting"
        assert "5/5" in conflicting.specification["unresolved_fields"][0]
        assert max_hangs.status == "needs_review"
        assert "total-load" in max_hangs.specification["unresolved_fields"][0]
        assert max_hangs.specification["load_basis"]["kind"] == "percent_of_total_load"
        assert max_hangs.specification["load_basis"]["percent"] == 90
        assert pinch.status == "needs_review"
        assert "external lifted load" in pinch.specification["unresolved_fields"][0]
        assert pinch.specification["load_basis"]["kind"] == "percent_of_external_lifted_load"
        assert pinch.specification["load_basis"]["excludes"] == ["body mass"]


async def test_ready_specs_preserve_verified_source_numbers_and_taxonomy(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        static = await _protocol(session, "static-holds-b")
        assert static.status == "ready"
        assert static.specification["original_category"] == "Conditioning"
        assert static.specification["original_subcategory"] == "Core"
        assert static.specification["duration"]["source_minutes"] == 15
        side_plank = static.specification["steps"][0]["children"][1]
        assert side_plank["sets"] == 2 and side_plank["seconds"] == 30
        assert side_plank["sides"] == ["left", "right"]

        interval = await _protocol(session, "1-on-1-off")
        step = interval.specification["steps"][0]
        assert interval.specification["original_category"] == "Endurance"
        assert interval.specification["original_subcategory"] == "Aerobic Capacity"
        assert (step["reps"], step["seconds"], step["rest_seconds"]) == (10, 60, 60)

        core = await _protocol(session, "6-minute-core")
        assert core.specification["duration"]["source_minutes"] == 10
        assert core.specification["equipment"]["required_all"] == ["exercise mat"]
        assert set(core.specification["equipment"]["optional_any"]) == {"dumbbell", "kettlebell"}

        boulder_warmup = await _protocol(session, "warm-up-protocol-bouldering")
        grip_step = boulder_warmup.specification["steps"][0]["children"][3]
        assert boulder_warmup.specification["duration"]["source_minutes"] == 25
        assert boulder_warmup.specification["equipment"]["required_all"] == ["bouldering wall"]
        assert (grip_step["reps"], grip_step["seconds"], grip_step["rest_seconds"]) == (9, 10, 50)

        extended = await _protocol(session, "extended-core-b")
        assert extended.specification["original_category"] == "Conditioning"
        assert set(extended.specification["equipment"]["required_any"]) == {
            "pull-up bar",
            "gymnastic rings",
        }
        assert "exercise mat" in extended.specification["equipment"]["source_icons"]

        linked = await _protocol(session, "linked-boulders")
        set_block = linked.specification["steps"][0]
        interrep = set_block["children"][0]
        assert (set_block["sets"], interrep["reps"], interrep["rest_seconds"]) == (3, 4, 120)
        assert set_block["rest_seconds"] == 300

        allowed_categories = {
            "Endurance",
            "Strength & Power",
            "Conditioning",
            "Power Endurance",
            "unknown",
        }
        rows = (await session.scalars(select(CatalogProtocol))).all()
        assert {row.specification["original_category"] for row in rows} <= allowed_categories


async def test_context_requires_explicit_equipment_and_level(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        # Known equipment alone is not enough for protocols with a flash-grade prerequisite.
        no_level = await catalog_context(
            session, profile={"equipment": ["route or circuit wall", "auto_belay"]}
        )
        assert "1 On 1 Off" not in {entry["title"] for entry in no_level}

        applicable = await catalog_context(
            session,
            profile={
                "equipment": ["route or circuit wall", "auto_belay"],
                "flash_grade": "5.10a",
                "locations": ["climbing_gym"],
            },
        )
        assert "1 On 1 Off" in {entry["title"] for entry in applicable}
        # Lack of a pull-up bar must not be silently assumed for extended core.
        assert "Extended Core - B" not in {entry["title"] for entry in applicable}

        from_experience = await catalog_context(
            session,
            profile={
                "equipment": ["route or circuit wall", "auto_belay"],
                "experience": [{"activity": "sport climbing", "flash_grade": "5.10a"}],
            },
        )
        assert "1 On 1 Off" in {entry["title"] for entry in from_experience}


async def test_auto_belay_implies_only_its_route_wall_and_keeps_grade_gate(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        missing_grade = await catalog_selection(
            session,
            profile={
                "equipment": ["auto-belay"],
                "locations": ["climbing_gym"],
                "goals": ["Build aerobic endurance for multipitch routes"],
            },
            limit=100,
        )
        assert "1-on-1-off" not in {item["slug"] for item in missing_grade["protocols"]}
        blocked = {item["slug"]: item for item in missing_grade["blocked_candidates"]}
        assert blocked["1-on-1-off"]["missing_profile"] == ["flash_grade"]
        assert blocked["1-on-1-off"]["selectable"] is False
        assert "protocol_id" not in blocked["1-on-1-off"]

        eligible = await catalog_context(
            session,
            profile={
                "equipment": ["autobelay"],
                "locations": ["climbing_gym"],
                "flash_grade": "6a",
            },
            limit=100,
        )
        slugs = {item["slug"] for item in eligible}
        assert {"1-on-1-off", "1-on-2-off", "continuity-5x3", "aero-4s"} <= slugs

        # The implication does not invent rope gear or a confirmed partner.
        session.add(
            CatalogProtocol(
                slug="rope-and-partner-only",
                version=1,
                title="Rope and partner only",
                category="climbing",
                status="ready",
                specification={
                    "equipment": {"required_all": ["rope_belay"]},
                    "eligibility": {"requires_partner": True},
                },
            )
        )
        await session.flush()
        eligible = await catalog_context(
            session,
            profile={"equipment": ["auto_belay"], "flash_grade": "6a"},
            limit=100,
        )
        assert "rope-and-partner-only" not in {item["slug"] for item in eligible}


async def test_catalog_ranking_prefers_goal_relevant_endurance_cards(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        protocols = await catalog_context(
            session,
            profile={
                "equipment": [
                    "auto_belay",
                    "bouldering wall",
                    "exercise mat",
                    "slab wall",
                ],
                "locations": ["climbing_gym"],
                "flash_grade": "6a",
                "onsight_grade": "5c",
                "goals": [
                    {"title": "Выносливость для лидирования участков мультипитча"}
                ],
            },
            limit=100,
        )
        first_five = [item["slug"] for item in protocols[:5]]
        assert set(first_five) <= {
            "1-on-1-off",
            "1-on-2-off",
            "50-50",
            "aero-4s",
            "continuity-5x3",
        }
        # Repeated selection is stable rather than database/alphabetical accident.
        repeated = await catalog_context(
            session,
            profile={
                "equipment": ["auto_belay", "exercise mat"],
                "flash_grade": "6a",
                "goals": ["aerobic endurance"],
            },
            limit=100,
        )
        repeated_again = await catalog_context(
            session,
            profile={
                "equipment": ["auto_belay", "exercise mat"],
                "flash_grade": "6a",
                "goals": ["aerobic endurance"],
            },
            limit=100,
        )
        assert [item["slug"] for item in repeated] == [item["slug"] for item in repeated_again]
        assert repeated[0]["category"] == "climbing"


def test_catalog_concepts_use_word_aware_russian_stems():
    assert "core" not in _concepts("Короткая тренировка")
    assert "technique" not in _concepts("Хочу немного полазать")
    assert "core" in _concepts("Укрепить кор и корпус")
    assert "technique" in _concepts("Техника ног и экономичные движения")
    assert "endurance" in _concepts("Развивать выносливость для мультипитча")


async def test_context_does_not_combine_location_bound_equipment(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        profile = {
            "flash_grade": "5.10a",
            "equipment": [
                {"name": "route or circuit wall", "available": True, "location": "gym-a"},
                {"name": "auto_belay", "available": True, "location": "gym-b"},
            ],
        }
        unknown_site = await catalog_context(session, profile=profile)
        assert "1 On 1 Off" not in {entry["title"] for entry in unknown_site}
        profile["session_location"] = "gym-a"
        gym_a = await catalog_context(session, profile=profile)
        assert "1 On 1 Off" not in {entry["title"] for entry in gym_a}
        profile["session_location"] = "gym-b"
        gym_b = await catalog_context(session, profile=profile)
        assert "1 On 1 Off" in {entry["title"] for entry in gym_b}


async def test_explicit_route_wall_unavailability_blocks_auto_belay_implication(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        protocols = await catalog_context(
            session,
            profile={
                "session_location": "gym-a",
                "flash_grade": "6a",
                "equipment": [
                    {"name": "auto_belay", "available": True, "location": "gym-a"},
                    {
                        "name": "route or circuit wall",
                        "available": False,
                        "location": "gym-a",
                    },
                ],
            },
            limit=100,
        )
        assert "1-on-1-off" not in {item["slug"] for item in protocols}


async def test_validate_blocks_freezes_snapshot_and_rejects_unsafe_changes(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        slab = await _protocol(session, "no-hands-slab")
        frozen = await validate_blocks(
            session,
            [{"kind": "climb", "protocol_id": slab.id, "protocol_version": 1, "parameters": []}],
            profile={"equipment": ["slab wall"]},
        )
        assert frozen[0]["kind"] == "catalog_protocol"
        assert frozen[0]["protocol_snapshot"]["version"] == 1
        slab.specification["purpose"] = ["changed after snapshot"]
        assert (
            frozen[0]["protocol_snapshot"]["specification"]["purpose"]
            != slab.specification["purpose"]
        )

        # Repeating a source default is allowed; only an actual change needs a
        # declared adjustment rule.
        slab.specification = {
            **slab.specification,
            "default_parameters": [{"name": "repetitions", "value": 6, "unit": "reps"}],
            "adjustable_parameters": [
                {"name": "repetitions", "unit": "reps", "minimum": 1, "maximum": 8}
            ],
        }
        repeated_default = await validate_blocks(
            session,
            [
                {
                    "protocol_id": slab.id,
                    "parameters": [{"name": "repetitions", "value": 6, "unit": "reps"}],
                }
            ],
            profile={"equipment": ["slab wall"]},
        )
        assert repeated_default[0]["parameters"] == [
            {"name": "repetitions", "value": 6, "unit": "reps"}
        ]

        with pytest.raises(ValueError, match="approved adjustment"):
            await validate_blocks(
                session,
                [{"protocol_id": slab.id, "parameters": [{"name": "sets", "value": 100}]}],
                profile={"equipment": ["slab wall"]},
            )
        with pytest.raises(ValueError, match="noncatalog"):
            await validate_blocks(session, [{"kind": "climb", "title": "Mystery"}], profile={})

        ad_hoc = await validate_blocks(
            session,
            [
                {
                    "kind": "mobility",
                    "title": "Custom warm-up",
                    "ad_hoc": True,
                    "instruction": "Keep it easy.",
                    "adaptation_reason": "No wall access.",
                }
            ],
            profile={},
        )
        assert ad_hoc[0]["instruction"] == "Keep it easy."
        assert ad_hoc[0]["adaptation_reason"] == "No wall access."


async def test_private_source_intake_dedupes_and_never_creates_workout(database):
    async with database.sessions() as session:
        before_workouts = await session.scalar(select(func.count()).select_from(Workout))
        athlete_id = await session.scalar(
            select(Message.athlete_id).where(Message.athlete_id.is_not(None))
        )
        assert athlete_id is not None
        message = Message(
            telegram_message_id=99101,
            telegram_chat_id=99101,
            telegram_user_id=99101,
            athlete_id=athlete_id,
            message_type="photo",
            telegram_timestamp=datetime.now(UTC),
            attachment_data={
                "telegram_payload": {
                    "photo": [{"file_unique_id": "private-source-dedupe"}],
                    "media_group_id": "album-1",
                }
            },
        )
        session.add(message)
        await session.flush()
        source = await receive_catalog_source(session, message=message, extracted={"kind": "photo"})
        duplicate = await receive_catalog_source(
            session, message=message, extracted={"kind": "photo"}
        )
        assert source.id == duplicate.id
        assert source.status == "extracted"
        assert source.album_id == "album-1"
        assert await session.scalar(select(func.count()).select_from(CatalogSource)) == 1
        assert await session.scalar(select(func.count()).select_from(Workout)) == before_workouts
