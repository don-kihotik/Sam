import pytest
from sqlalchemy import select

from app import catalog
from app.catalog import catalog_context, seed_catalog, validate_blocks
from app.db.models import CatalogProtocol, Plan
from app.planning import render_training_plan

_PROFILE = {
    "equipment": ["route or circuit wall", "auto_belay", "circuit_board"],
    "locations": ["climbing_gym"],
    "flash_grade": "5.10",
}


async def _protocol(session, slug: str) -> CatalogProtocol:
    row = await session.scalar(select(CatalogProtocol).where(CatalogProtocol.slug == slug))
    assert row is not None
    return row


async def test_sam_volume_reduction_freezes_source_and_exposes_effective_recipe(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        one_on_one = await _protocol(session, "1-on-1-off")
        frozen = await validate_blocks(
            session,
            [
                {
                    "protocol_id": one_on_one.id,
                    "parameters": [{"name": "sam_volume_reps", "value": 6, "unit": "count"}],
                    "adaptation_reason": "short recovery window",
                }
            ],
            profile=_PROFILE,
        )
        block = frozen[0]
        assert block["protocol_snapshot"]["specification"]["steps"][0]["reps"] == 10
        assert block["effective_steps"][0]["reps"] == 6
        assert block["effective_duration"]["kind"] == "computed_timer"
        assert block["effective_duration"]["minutes"] == 12.0
        plan = Plan(
            id=10,
            athlete_id=1,
            scope="session",
            start_date=one_on_one.created_at.date(),
            content={"blocks": [block]},
        )
        rendered = render_training_plan(plan, detailed=True)
        assert "Эффективное назначение Sam" in rendered
        assert "Оригинальный рецепт источника" in rendered
        continuity = await _protocol(session, "continuity-5x3")
        continuity_block = (
            await validate_blocks(
                session,
                [
                    {
                        "protocol_id": continuity.id,
                        "parameters": [{"name": "sam_volume_sets", "value": 2, "unit": "count"}],
                        "adaptation_reason": "reduced available time",
                    }
                ],
                profile=_PROFILE,
            )
        )[0]
        assert continuity_block["effective_steps"][0]["sets"] == 2
        assert continuity_block["protocol_snapshot"]["specification"]["steps"][0]["sets"] == 3
        assert continuity_block["effective_duration"]["minutes"] == 13.0
        assert "excludes warmup/transitions" in continuity_block["effective_duration"]["basis"]


async def test_sam_volume_policy_rejects_unsafe_or_unexplained_changes(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        one_on_one = await _protocol(session, "1-on-1-off")
        base = {
            "protocol_id": one_on_one.id,
            "parameters": [{"name": "sam_volume_reps", "value": 6, "unit": "count"}],
        }
        with pytest.raises(ValueError, match="adaptation_reason"):
            await validate_blocks(session, [base], profile=_PROFILE)
        for value in (11, 5.5):
            with pytest.raises(ValueError):
                await validate_blocks(
                    session,
                    [
                        {
                            **base,
                            "parameters": [
                                {"name": "sam_volume_reps", "value": value, "unit": "count"}
                            ],
                            "adaptation_reason": "reason",
                        }
                    ],
                    profile=_PROFILE,
                )
        unready = await session.scalar(
            select(CatalogProtocol).where(CatalogProtocol.status != "ready")
        )
        assert unready is not None
        with pytest.raises(ValueError, match="unready"):
            await validate_blocks(
                session, [{"protocol_id": unready.id, "adaptation_reason": "reason"}], profile={}
            )


async def test_catalog_context_exposes_policy_as_sam_not_source_permission(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        context = await catalog_context(session, profile=_PROFILE, limit=100)
    cards = {card["slug"]: card for card in context}
    for slug in ("1-on-1-off", "1-on-2-off", "continuity-5x3"):
        policy = cards[slug]["specification"]["sam_adaptation_policy"]
        assert policy["provenance"] == "sam_adaptation_policy_not_source"
    assert cards["1-on-1-off"]["parameters"][0]["value"] == 10


def _versioned_entry(*, version: int, status: str, title: str) -> dict:
    return {
        "slug": "versioned-test",
        "version": version,
        "title": title,
        "category": "conditioning",
        "status": status,
        "original_category": "Fixture",
        "steps": [{"type": "action", "label": "Keep this exact source"}],
    }


async def test_seed_keeps_immutable_versions_and_context_uses_only_latest(database, monkeypatch):
    entries = [
        _versioned_entry(version=1, status="ready", title="v1 ready"),
        _versioned_entry(version=2, status="ready", title="v2 ready"),
    ]
    monkeypatch.setattr(catalog, "_inventory", lambda: entries)
    async with database.sessions() as session:
        await seed_catalog(session)
        await seed_catalog(session)
        rows = (
            await session.scalars(
                select(CatalogProtocol)
                .where(CatalogProtocol.slug == "versioned-test")
                .order_by(CatalogProtocol.version)
            )
        ).all()
        assert [(row.version, row.title) for row in rows] == [(1, "v1 ready"), (2, "v2 ready")]
        entries[1]["title"] = "edited without version bump"
        await seed_catalog(session)
        assert (await session.get(CatalogProtocol, rows[1].id)).title == "v2 ready"
        context = await catalog_context(session, profile={}, limit=20)
        assert [card["title"] for card in context if card["slug"] == "versioned-test"] == [
            "v2 ready"
        ]


async def test_latest_unready_version_hides_old_ready_source(database, monkeypatch):
    monkeypatch.setattr(
        catalog,
        "_inventory",
        lambda: [
            _versioned_entry(version=1, status="ready", title="old ready"),
            _versioned_entry(version=2, status="draft", title="new draft"),
        ],
    )
    async with database.sessions() as session:
        await seed_catalog(session)
        context = await catalog_context(session, profile={}, limit=20)
    assert not any(card["slug"] == "versioned-test" for card in context)
