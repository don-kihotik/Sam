from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.catalog import seed_catalog, validate_blocks
from app.db.models import Athlete, CatalogProtocol, Plan
from app.planning import block_duration_minutes, render_training_plan, session_duration_minutes
from app.training_commands import handle_training_command


def _adapted_plan(*, plan_id: int = 44, athlete_id: int = 1) -> Plan:
    block = {
        "kind": "catalog_protocol",
        "protocol_id": 7,
        "protocol_version": 3,
        "title": "1 On 1 Off",
        "parameters": [{"name": "sam_volume_reps", "value": 6, "unit": "count"}],
        "instruction": "Используй auto_belay или автобейл.",
        "adaptation_reason": "Короткое окно восстановления",
        "effective_steps": [
            {
                "label": "Лёгкий маршрут",
                "reps": 6,
                "seconds": 60,
                "rest_seconds": 60,
            }
        ],
        "effective_duration": {"kind": "computed_timer", "minutes": 12},
        "protocol_snapshot": {
            "slug": "1-on-1-off",
            "version": 3,
            "specification": {
                "steps": [
                    {
                        "label": "Лёгкий маршрут",
                        "reps": 10,
                        "seconds": 60,
                        "rest_seconds": 60,
                    }
                ],
                "restrictions": [
                    {"text": "Остановись при боли в пальцах."},
                    {"text": "Остановись при боли в пальцах."},
                ],
            },
        },
    }
    return Plan(
        id=plan_id,
        athlete_id=athlete_id,
        scope="session",
        status="active",
        start_date=date(2026, 9, 24),
        end_date=date(2026, 9, 24),
        source_message_ids=[91],
        evidence=["снизить объём после тяжёлой недели"],
        content={
            "version": 5,
            "summary": "Аэробная выносливость",
            "blocks": [block],
            "guardrails": ["Остановись при боли в пальцах."],
        },
    )


def test_compact_plan_is_human_readable_and_uses_effective_prescription_only():
    plan = _adapted_plan()

    rendered = render_training_plan(plan)

    assert "Тренировка на 24 сентября, четверг" in rendered
    assert "Цель: Аэробная выносливость" in rendered
    assert "Ориентир по времени: около 12 мин." in rendered
    assert "повторы: 6" in rendered
    assert "6 повторов" in rendered
    assert "10 повторов" not in rendered
    assert "автостраховка" in rendered
    assert "автобейл" not in rendered and "auto_belay" not in rendered
    assert rendered.count("Важно:") == 1
    assert rendered.count("Остановись при боли в пальцах.") == 1
    assert "#44" not in rendered and "версия 5" not in rendered
    assert "Оригинальный рецепт" not in rendered
    assert "Короткое окно восстановления" not in rendered
    assert "1. Интервалы: 1 мин лазания / 1 мин отдыха (каталог)" in rendered
    # Rendering is presentation-only; the immutable snapshot remains unchanged.
    assert plan.content["blocks"][0]["instruction"] == "Используй auto_belay или автобейл."


def test_detailed_markdown_keeps_identifiers_provenance_and_both_recipes():
    rendered = render_training_plan(_adapted_plan(), detailed=True)

    assert rendered.startswith("# Тренировка #44 · версия 5")
    assert "Исходные сообщения: #91" in rendered
    assert "Основание: снизить объём после тяжёлой недели" in rendered
    assert "Источник: каталог #7, 1-on-1-off v3" in rendered
    assert "Эффективное назначение Sam:" in rendered
    assert "6 повторов" in rendered
    assert "Оригинальный рецепт источника:" in rendered
    assert "10 повторов" in rendered
    assert "автостраховка" in rendered


def test_ready_route_endurance_cards_are_readable_in_russian_without_mutating_source():
    source_titles = [
        "1 On 1 Off",
        "1 On 2 Off",
        "Continuity 5x3",
        "Aero 4's",
        "50-50",
        "Warm Up Protocol - Route Climbing",
    ]
    source_steps = [
        "Easy route or circuit interval",
        "Easy route or circuit interval",
        "Continuous easy climbing",
        "Easy route or circuit",
        "30-move lap",
        "Route warm-up",
    ]
    plan = Plan(
        id=45,
        athlete_id=1,
        scope="session",
        status="active",
        visibility="private",
        start_date=date(2026, 9, 24),
        end_date=date(2026, 9, 24),
        content={
            "summary": "Аэробная работа на трассах",
            "blocks": [
                {
                    "kind": "catalog_protocol",
                    "protocol_id": index,
                    "title": title,
                    "parameters": [],
                    "protocol_snapshot": {
                        "slug": f"source-{index}",
                        "version": 1,
                        "specification": {
                            "steps": [{"type": "action", "label": step}],
                        },
                    },
                }
                for index, (title, step) in enumerate(zip(source_titles, source_steps), start=1)
            ],
        },
        evidence=[],
        source_message_ids=[],
    )

    rendered = render_training_plan(plan)

    assert "Интервалы: 1 мин лазания / 1 мин отдыха" in rendered
    assert "Интервалы: 1 мин лазания / 2 мин отдыха" in rendered
    assert "Непрерывное лёгкое лазание 5×3" in rendered
    assert "Аэробные четвёрки" in rendered
    assert "50/50: лёгкие и сложные движения" in rendered
    assert "Разминка перед лазанием на трассах" in rendered
    assert "Лёгкая трасса или круг" in rendered
    assert "Круг из 30 движений" in rendered
    assert plan.content["blocks"][0]["title"] == "1 On 1 Off"
    assert plan.content["blocks"][4]["protocol_snapshot"]["specification"]["steps"][0][
        "label"
    ] == "30-move lap"


def test_compact_plan_never_hides_untranslated_source_safety_restriction():
    plan = Plan(
        id=46,
        athlete_id=1,
        scope="session",
        status="active",
        visibility="private",
        start_date=date(2026, 9, 24),
        end_date=date(2026, 9, 24),
        content={
            "summary": "Безопасная проверка",
            "blocks": [
                {
                    "kind": "catalog_protocol",
                    "protocol_id": 99,
                    "title": "Source protocol",
                    "parameters": [],
                    "protocol_snapshot": {
                        "slug": "source-protocol",
                        "version": 1,
                        "specification": {
                            "steps": [],
                            "restrictions": [
                                {
                                    "kind": "safety",
                                    "text": "Stop immediately if the shoulder becomes painful.",
                                }
                            ],
                        },
                    },
                }
            ],
        },
        evidence=[],
        source_message_ids=[],
    )

    rendered = render_training_plan(plan)

    assert "Важно: Stop immediately if the shoulder becomes painful." in rendered

    week = Plan(
        id=47,
        athlete_id=1,
        scope="week",
        status="active",
        visibility="private",
        start_date=date(2026, 9, 24),
        end_date=date(2026, 9, 30),
        content={
            "summary": "Безопасная неделя",
            "sessions": [
                {
                    "id": 48,
                    "date": "2026-09-24",
                    "title": "Проверка ограничения",
                    "blocks": plan.content["blocks"],
                }
            ],
        },
        evidence=[],
        source_message_ids=[],
    )
    assert "Важно: Stop immediately if the shoulder becomes painful." in render_training_plan(
        week
    )
    assert "Ограничение источника: Stop immediately if the shoulder becomes painful." in (
        render_training_plan(week, detailed=True)
    )


@pytest.mark.asyncio
async def test_export_uses_detailed_markdown_not_compact_chat_view(database, settings):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        plan = _adapted_plan(athlete_id=athlete.id)
        session.add(plan)
        await session.flush()
        result = await handle_training_command(
            session=session,
            athlete=athlete,
            message=SimpleNamespace(raw_text=f"/export {plan.id}", telegram_chat_id=101),
            settings=settings,
            today=date(2026, 9, 24),
        )

    document = result.payload["document"]
    assert document["filename"] == f"sam-plan-{plan.id}.md"
    assert result.text == "Подробности занятия — в файле."
    assert "# Тренировка" in document["text"]
    assert "Оригинальный рецепт источника:" in document["text"]


def test_compact_goal_duration_and_source_labels_do_not_expose_internal_values():
    plan = Plan(
        id=45,
        athlete_id=1,
        scope="session",
        start_date=date(2026, 9, 25),
        content={
            "current_focus": ["climbing endurance", "home"],
            "blocks": [
                {
                    "kind": "catalog_protocol",
                    "title": "Source protocol",
                    "protocol_snapshot": {
                        "specification": {
                            "duration": {"estimated_minutes": 20},
                            "steps": [{"label": "Easy route", "reps": 10, "rest_seconds": 60}]
                        }
                    },
                },
                {
                    "kind": "ad_hoc",
                    "title": "Силовой блок",
                    "ad_hoc": True,
                    "source": "ad_hoc_noncatalog",
                    "parameters": [{"name": "duration_seconds", "value": 600, "unit": "seconds"}],
                },
            ],
        },
    )

    rendered = render_training_plan(plan)

    assert "Цель: выносливость в скалолазании, дома" in rendered
    assert "['climbing endurance'" not in rendered
    # Explicit seconds become ten minutes; they are never treated as 600 minutes.
    assert "Ориентир по времени: около 30 мин." in rendered
    assert "Известная часть по времени" not in rendered
    assert "Source protocol (каталог)" in rendered
    assert "Силовой блок (индивидуальный)" in rendered
    assert "Easy route" not in rendered
    assert "10 повторов" in rendered and "отдых 1 мин" in rendered
    assert "длительность: 600 сек" in rendered
    assert block_duration_minutes(plan.content["blocks"][1]) == 10
    assert session_duration_minutes(plan.content["blocks"]) == 30


def test_detailed_ad_hoc_block_has_explicit_non_catalog_provenance():
    plan = Plan(
        id=46,
        athlete_id=1,
        scope="session",
        start_date=date(2026, 9, 25),
        content={
            "blocks": [
                {
                    "kind": "ad_hoc",
                    "title": "Силовой блок",
                    "ad_hoc": True,
                    "source": "ad_hoc_noncatalog",
                }
            ]
        },
    )

    assert "Источник: личный блок (не каталог)" in render_training_plan(plan, detailed=True)


@pytest.mark.asyncio
async def test_compact_seeded_warmup_and_drop_cards_name_actions_and_keep_doses(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        warmup = await session.scalar(
            select(CatalogProtocol).where(CatalogProtocol.slug == "warm-up-protocol-bouldering")
        )
        drop = await session.scalar(
            select(CatalogProtocol).where(CatalogProtocol.slug == "drop-intervals")
        )
        assert warmup is not None and drop is not None
        blocks = await validate_blocks(
            session,
            [{"protocol_id": warmup.id}, {"protocol_id": drop.id}],
            profile={"locations": ["climbing_gym"], "equipment": ["bouldering wall"]},
        )

    plan = Plan(
        id=47,
        athlete_id=1,
        scope="session",
        start_date=date(2026, 9, 25),
        content={"summary": "Подготовка и выносливость", "blocks": blocks},
    )
    rendered = render_training_plan(plan)

    assert "Разминка перед боулдерингом" in rendered
    assert "Кардио-разминка; Выбери кардио-упражнение.; 5 мин" in rendered
    assert "Разминка хвата; Используй разные хваты с низкой интенсивностью.; 9 повторов; 10 сек; отдых 50 сек" in rendered
    assert "Интервалы со снижением сложности" in rendered
    assert "Серия; 6 подходов" in rendered
    assert "Самый сложный из шести близких боулдеров" in rendered
    assert "Пройди ещё пять более лёгких боулдеров с минимальным отдыхом" in rendered
    assert "Отдохни между сериями.; отдых 2 мин" in rendered
    assert "Снизь сложность, если срываешься или слишком забиваются предплечья." in rendered
    assert "Reduce grade if failing" not in rendered
    assert "300 сек" not in rendered


def test_session_duration_requires_every_block_to_be_known():
    known = {"parameters": [{"name": "duration", "value": 20, "unit": "min"}]}
    unknown = {"kind": "ad_hoc", "title": "Без длительности"}

    assert block_duration_minutes(known) == 20
    assert session_duration_minutes([known, unknown]) is None
