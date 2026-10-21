from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app.analytics import analytics_windows
from app.db.models import Athlete, Message, Plan, Workout
from app.planning import (
    _confirmed_candidate,
    begin_log,
    confirm_planned_session,
    latest_session_plan,
    render_training_plan,
    save_plan,
)
from app.records import save_session
from app.schemas import (
    ActualActivityCandidate,
    ActualSetCandidate,
    PlanCandidate,
    PlannedBlockCandidate,
    PlannedSessionCandidate,
    PlanParameter,
    WeeklyScheduleItem,
    WorkoutCandidate,
)


async def _message(session, athlete_id: int, telegram_id: int, text: str) -> Message:
    message = Message(
        athlete_id=athlete_id,
        telegram_chat_id=1000 + athlete_id,
        telegram_message_id=telegram_id,
        direction="incoming",
        raw_text=text,
        normalized_text=text,
        telegram_timestamp=datetime(2026, 9, 24, tzinfo=UTC),
    )
    session.add(message)
    await session.flush()
    return message


async def test_scoped_plan_keeps_program_and_materializes_session(database):
    day = date(2026, 9, 24)
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        source = await _message(session, alexey.id, 801, "план на неделю")
        program = await save_plan(
            session,
            athlete_id=alexey.id,
            candidate=PlanCandidate(action="create", scope="program", summary="base"),
            source_message_id=source.id,
            evidence=["план"],
            today=day,
        )
        week = await save_plan(
            session,
            athlete_id=alexey.id,
            candidate=PlanCandidate(
                action="create",
                scope="week",
                parent_plan_id=program.id,
                start_date=day,
                sessions=[
                    PlannedSessionCandidate(
                        date=day,
                        title="strength",
                        blocks=[
                            PlannedBlockCandidate(kind="strength", ad_hoc=True, title="pull-ups")
                        ],
                    )
                ],
                adaptation_reason="fresh recovery report",
            ),
            source_message_id=source.id,
            evidence=["план"],
            today=day,
        )
        child = await latest_session_plan(session, athlete_id=alexey.id, day=day)
        assert child and child.parent_id == week.id
        assert (await session.get(Plan, program.id)).status == "active"


async def test_actual_sets_are_evidence_checked_and_conditioned_analytics(database):
    day = date(2026, 9, 24)
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        message = await _message(session, alexey.id, 802, "Сделал 3 подхода по 5 с 20 кг")
        candidate = WorkoutCandidate(
            present=True,
            date=day,
            workout_type="strength",
            evidence=["Сделал 3 подхода по 5 с 20 кг"],
            activities=[
                ActualActivityCandidate(
                    kind="strength",
                    exercise="pull-up",
                    condition="open grip",
                    evidence=["Сделал 3 подхода по 5 с 20 кг"],
                    sets=[
                        ActualSetCandidate(
                            set_number=1, reps=5, weight_kg=20, evidence=["5 с 20 кг"]
                        )
                    ],
                )
            ],
        )
        workout, status = await save_session(
            session,
            athlete_id=alexey.id,
            message=message,
            candidate=candidate,
            source_text=message.raw_text or "",
            today=day,
        )
        assert status == "created" and workout
        # The fabricated 30 kg is stripped even though the enclosing activity is real.
        assert workout.structured_details["activities"][0]["sets"][0]["weight_kg"] == 20
        metrics = await analytics_windows(session, alexey.id, day, windows=(7,))
        group = metrics["7"]["actual_activities"]["strength_by_exercise_and_condition"][
            "pull-up | open grip"
        ]
        assert group["volume_kg_reps"] == 100
        assert group["comparable_volume_sets"] == 1


async def test_plan_numbers_require_explicit_begin_log_confirmation(database):
    day = date(2026, 9, 24)
    now = datetime(2026, 9, 24, 12, tzinfo=UTC)
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        source = await _message(session, alexey.id, 803, "план")
        plan = await save_plan(
            session,
            athlete_id=alexey.id,
            candidate=PlanCandidate(
                action="create",
                scope="session",
                start_date=day,
                blocks=[
                    PlannedBlockCandidate(
                        kind="strength",
                        ad_hoc=True,
                        title="pull-up",
                        parameters=[
                            PlanParameter(name="sets", value=2),
                            PlanParameter(name="reps", value=5),
                            PlanParameter(name="weight_kg", value=10, unit="kg"),
                        ],
                    )
                ],
            ),
            source_message_id=source.id,
            evidence=["план"],
            today=day,
        )
        confirm = await _message(session, alexey.id, 804, "подтверждаю")
        confirm.message_type = "callback"
        confirm.processing_data = {"training_action": "confirm_plan", "plan_id": plan.id}
        workout, status = await confirm_planned_session(
            session, athlete_id=alexey.id, plan_id=plan.id, message=confirm, today=day, now=now
        )
        assert workout is None and status == "needs_log_confirmation"
        prompt = await begin_log(session, athlete_id=alexey.id, plan_id=plan.id, now=now)
        assert "«сделал/всё»" in prompt
        workout, status = await confirm_planned_session(
            session, athlete_id=alexey.id, plan_id=plan.id, message=confirm, today=day, now=now
        )
        assert status == "created" and workout
        assert len(workout.structured_details["activities"][0]["sets"]) == 2
        assert workout.structured_details["activities"][0]["sets"][0]["weight_kg"] == 10
        replay = await _message(session, alexey.id, 806, "подтверждаю")
        replay.message_type = "callback"
        replay.processing_data = {"training_action": "confirm_plan", "plan_id": plan.id}
        duplicate, duplicate_status = await confirm_planned_session(
            session, athlete_id=alexey.id, plan_id=plan.id, message=replay, today=day, now=now
        )
        assert duplicate is None and duplicate_status == "needs_log_confirmation"
        await begin_log(session, athlete_id=alexey.id, plan_id=plan.id, now=now)
        reopened = await _message(session, alexey.id, 807, "подтверждаю")
        reopened.message_type = "callback"
        reopened.processing_data = {"training_action": "confirm_plan", "plan_id": plan.id}
        existing, existing_status = await confirm_planned_session(
            session, athlete_id=alexey.id, plan_id=plan.id, message=reopened, today=day, now=now
        )
        assert existing_status == "already_recorded" and existing and existing.id == workout.id
        assert len(existing.structured_details["activities"][0]["sets"]) == 2


async def test_plan_source_cannot_cross_athletes(database):
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        andrey = await session.scalar(select(Athlete).where(Athlete.name == "Andrey"))
        assert alexey and andrey
        source = await _message(session, alexey.id, 805, "план")
        with pytest.raises(ValueError, match="source is not owned"):
            await save_plan(
                session,
                athlete_id=andrey.id,
                candidate=PlanCandidate(action="create", scope="week"),
                source_message_id=source.id,
                evidence=["план"],
                today=date(2026, 9, 24),
            )


async def test_run_units_and_sentence_punctuation_preserve_actual_metrics(database):
    day = date(2026, 9, 24)
    text = "Сегодня пробежал 5 км за 30 минут."
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        message = await _message(session, alexey.id, 808, text)
        workout, status = await save_session(
            session,
            athlete_id=alexey.id,
            message=message,
            source_text=text,
            today=day,
            candidate=WorkoutCandidate(
                present=True,
                date=day,
                workout_type="run",
                duration_minutes=30,
                evidence=[text],
                activities=[
                    ActualActivityCandidate(
                        kind="run", duration_minutes=30, distance_m=5000, evidence=[text]
                    )
                ],
            ),
        )
        assert status == "created" and workout and workout.duration_minutes == 30
        run = workout.structured_details["activities"][0]
        assert run["distance_m"] == 5000 and run["duration_minutes"] == 30


async def test_run_analytics_counts_one_workout_with_two_same_condition_segments_once(database):
    day = date(2026, 9, 24)
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        session.add(
            Workout(
                athlete_id=alexey.id,
                date=day,
                type="run",
                status="active",
                structured_details={
                    "activities": [
                        {
                            "kind": "run",
                            "condition": "road",
                            "duration_minutes": 10,
                            "distance_m": 1600,
                        },
                        {
                            "kind": "run",
                            "condition": "road",
                            "duration_minutes": 5,
                            "distance_m": 800,
                        },
                    ]
                },
            )
        )
        await session.flush()
        metrics = await analytics_windows(session, alexey.id, day, windows=(7,))
        road = metrics["7"]["actual_activities"]["run_by_condition"]["road"]
        assert road == {
            "condition": "road",
            "sessions": 1,
            "reported_activities": 2,
            "duration_known_sessions": 1,
            "distance_known_sessions": 1,
            "duration_minutes": 15,
            "distance_m": 2400.0,
        }


async def test_explicit_plan_never_merges_an_unlinked_same_day_workout(database):
    day = date(2026, 9, 24)
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        source = await _message(session, alexey.id, 809, "сила")
        plan = await save_plan(
            session,
            athlete_id=alexey.id,
            candidate=PlanCandidate(action="create", scope="session", start_date=day),
            source_message_id=source.id,
            evidence=["сила"],
            today=day,
        )
        morning_run = Workout(athlete_id=alexey.id, date=day, type="run", status="active")
        session.add(morning_run)
        await session.flush()
        report = await _message(session, alexey.id, 810, "Сделал 5 повторов")
        saved, status = await save_session(
            session,
            athlete_id=alexey.id,
            message=report,
            source_text=report.raw_text or "",
            today=day,
            candidate=WorkoutCandidate(
                present=True,
                session_action="append",
                plan_id=plan.id,
                date=day,
                evidence=["Сделал 5 повторов"],
                activities=[
                    ActualActivityCandidate(
                        kind="strength",
                        exercise="pull-up",
                        evidence=["Сделал 5 повторов"],
                        sets=[ActualSetCandidate(reps=5, evidence=["5 повторов"])],
                    )
                ],
            ),
        )
        assert status == "created" and saved and saved.id != morning_run.id
        assert morning_run.plan_id is None and saved.plan_id == plan.id


async def test_scoped_updates_infer_target_parent_and_reject_ambiguous_or_private_group(database):
    day = date(2026, 9, 24)
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        source = await _message(session, alexey.id, 811, "план")
        program = await save_plan(
            session,
            athlete_id=alexey.id,
            candidate=PlanCandidate(action="create", scope="program"),
            source_message_id=source.id,
            evidence=["план"],
            today=day,
        )
        session_plan = await save_plan(
            session,
            athlete_id=alexey.id,
            candidate=PlanCandidate(
                action="create", scope="session", parent_plan_id=program.id, start_date=day
            ),
            source_message_id=source.id,
            evidence=["план"],
            today=day,
        )
        replacement = await save_plan(
            session,
            athlete_id=alexey.id,
            candidate=PlanCandidate(
                action="update",
                scope="session",
                target_plan_id=session_plan.id,
                start_date=day,
                summary="changed",
            ),
            source_message_id=source.id,
            evidence=["план"],
            today=day,
        )
        assert replacement.parent_id == program.id and replacement.supersedes_id == session_plan.id
        with pytest.raises(ValueError, match="private (target|parent)"):
            await save_plan(
                session,
                athlete_id=alexey.id,
                candidate=PlanCandidate(
                    action="update", scope="session", target_plan_id=replacement.id, start_date=day
                ),
                source_message_id=source.id,
                evidence=["план"],
                today=day,
                visibility="group",
            )
        first = await save_plan(
            session,
            athlete_id=alexey.id,
            candidate=PlanCandidate(action="create", scope="session", start_date=day),
            source_message_id=source.id,
            evidence=["план"],
            today=day,
        )
        second = await save_plan(
            session,
            athlete_id=alexey.id,
            candidate=PlanCandidate(action="create", scope="session", start_date=day),
            source_message_id=source.id,
            evidence=["план"],
            today=day,
        )
        assert first.id != second.id
        with pytest.raises(ValueError, match="ambiguous"):
            await save_plan(
                session,
                athlete_id=alexey.id,
                candidate=PlanCandidate(action="update", scope="session", start_date=day),
                source_message_id=source.id,
                evidence=["план"],
                today=day,
            )


async def test_explicit_sessions_win_over_weekly_labels_and_rests_are_not_sessions(database):
    day = date(2026, 9, 21)  # Monday
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        source = await _message(session, alexey.id, 812, "неделя")
        week = await save_plan(
            session,
            athlete_id=alexey.id,
            candidate=PlanCandidate(
                action="create",
                scope="week",
                start_date=day,
                weekly_schedule=[
                    WeeklyScheduleItem(day="Tuesday", activity="legacy easy"),
                    WeeklyScheduleItem(day="Wednesday", activity="rest day"),
                ],
                sessions=[
                    PlannedSessionCandidate(date=date(2026, 9, 22), title="concrete Tuesday")
                ],
            ),
            source_message_id=source.id,
            evidence=["неделя"],
            today=day,
        )
        children = (await session.scalars(select(Plan).where(Plan.parent_id == week.id))).all()
        assert len(children) == 1 and children[0].content["summary"] == "concrete Tuesday"


async def test_scope_validation_preserves_private_program_on_group_week_update(database):
    day = date(2026, 9, 24)
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        source = await _message(session, alexey.id, 813, "план")
        private_program = await save_plan(
            session,
            athlete_id=alexey.id,
            candidate=PlanCandidate(action="create", scope="program"),
            source_message_id=source.id,
            evidence=["план"],
            today=day,
            visibility="private",
        )
        group_week = await save_plan(
            session,
            athlete_id=alexey.id,
            candidate=PlanCandidate(action="update", scope="week", start_date=day),
            source_message_id=source.id,
            evidence=["план"],
            today=day,
            visibility="group",
        )
        assert private_program.status == "active"
        assert group_week.visibility == "group"
        private_program.status = "superseded"
        with pytest.raises(ValueError, match="parent plan is not active"):
            await save_plan(
                session,
                athlete_id=alexey.id,
                candidate=PlanCandidate(
                    action="create",
                    scope="session",
                    parent_plan_id=private_program.id,
                    start_date=day,
                ),
                source_message_id=source.id,
                evidence=["план"],
                today=day,
            )
        with pytest.raises(ValueError, match="same day"):
            await save_plan(
                session,
                athlete_id=alexey.id,
                candidate=PlanCandidate(
                    action="create", scope="session", start_date=day, end_date=date(2026, 9, 25)
                ),
                source_message_id=source.id,
                evidence=["план"],
                today=day,
            )


def test_confirmation_rejects_ranges_and_nested_repeat_without_throwing():
    day = date(2026, 9, 24)
    range_plan = Plan(
        athlete_id=1,
        scope="session",
        start_date=day,
        end_date=day,
        content={
            "blocks": [
                {
                    "kind": "strength",
                    "title": "pull-up",
                    "parameters": [{"name": "sets", "value": 3}, {"name": "reps", "value": "5-8"}],
                }
            ]
        },
    )
    nested_plan = Plan(
        athlete_id=1,
        scope="session",
        start_date=day,
        end_date=day,
        content={
            "blocks": [
                {
                    "kind": "catalog_protocol",
                    "category": "conditioning",
                    "title": "nested",
                    "parameters": [],
                    "protocol_snapshot": {
                        "specification": {
                            "steps": [
                                {
                                    "type": "repeat",
                                    "sets": 2,
                                    "reps": 3,
                                    "children": [
                                        {"type": "repeat", "sets": 2, "reps": 4},
                                    ],
                                },
                            ]
                        }
                    },
                }
            ]
        },
    )
    assert _confirmed_candidate(range_plan, "shown", None) is None
    assert _confirmed_candidate(nested_plan, "shown", None) is None


def test_confirmation_accepts_integral_float_scalars_but_rejects_composites_and_adapted_fallback():
    day = date(2026, 9, 24)
    scalar_plan = Plan(
        athlete_id=1,
        scope="session",
        start_date=day,
        end_date=day,
        content={
            "blocks": [
                {
                    "kind": "strength",
                    "title": "pull-up",
                    "parameters": [{"name": "sets", "value": 2.0}, {"name": "reps", "value": 5.0}],
                }
            ]
        },
    )
    confirmed = _confirmed_candidate(scalar_plan, "shown", None)
    assert confirmed is not None and len(confirmed.activities[0].sets) == 2
    composite_plan = Plan(
        athlete_id=1,
        scope="session",
        start_date=day,
        end_date=day,
        content={
            "blocks": [
                {
                    "kind": "catalog_protocol",
                    "category": "conditioning",
                    "title": "mixed",
                    "parameters": [],
                    "protocol_snapshot": {
                        "specification": {
                            "default_parameters": [],
                            "steps": [
                                {
                                    "type": "repeat",
                                    "sets": 2,
                                    "reps": 5,
                                    "children": [{"type": "action", "label": "other"}],
                                },
                            ],
                        }
                    },
                }
            ]
        },
    )
    adapted_plan = Plan(
        athlete_id=1,
        scope="session",
        start_date=day,
        end_date=day,
        content={
            "blocks": [
                {
                    "kind": "catalog_protocol",
                    "category": "conditioning",
                    "title": "adapted",
                    "parameters": [{"name": "effort", "value": 2}],
                    "protocol_snapshot": {
                        "specification": {
                            "default_parameters": [],
                            "steps": [
                                {"type": "repeat", "sets": 2, "reps": 5},
                            ],
                        }
                    },
                }
            ]
        },
    )
    assert _confirmed_candidate(composite_plan, "shown", None) is None
    assert _confirmed_candidate(adapted_plan, "shown", None) is None


def test_render_keeps_phase_location_and_executable_step_detail():
    plan = Plan(
        id=42,
        athlete_id=1,
        scope="session",
        start_date=date(2026, 9, 24),
        end_date=date(2026, 9, 24),
        content={
            "summary": "run session",
            "location": "track",
            "notes": "easy pace",
            "phases": [{"title": "base", "start_date": "2026-09-01", "objective": "aerobic"}],
            "blocks": [
                {
                    "kind": "catalog_protocol",
                    "title": "Intervals",
                    "parameters": [{"name": "duration_minutes", "value": 20, "unit": "min"}],
                    "instruction": "Keep form smooth.",
                    "adaptation_reason": "short on time",
                    "protocol_snapshot": {
                        "specification": {
                            "default_parameters": [],
                            "steps": [
                                {
                                    "type": "repeat",
                                    "sets": 2,
                                    "seconds_min": 30,
                                    "seconds_max": 45,
                                    "rest_seconds_min": 60,
                                    "rest_seconds_max": 90,
                                    "sides": "each",
                                }
                            ],
                        }
                    },
                }
            ],
        },
    )
    rendered = render_training_plan(plan, detailed=True)
    for text in (
        "Место: track",
        "Заметка: easy pace",
        "Фаза: base",
        "Keep form smooth.",
        "short on time",
        "сек минимум",
        "стороны: each",
        "Оригинальный рецепт источника",
    ):
        assert text in rendered


async def test_single_nested_session_is_promoted_and_duration_alias_is_narrowly_normalized(
    database,
):
    day = date(2026, 9, 24)
    candidate = PlanCandidate(
        action="create",
        scope="session",
        sessions=[
            PlannedSessionCandidate(
                date=day,
                title="walk",
                location="park",
                notes="easy",
                blocks=[
                    PlannedBlockCandidate(
                        kind="run",
                        ad_hoc=True,
                        title="walk",
                        parameters=[PlanParameter(name="duration", value=20, unit="min")],
                    )
                ],
            )
        ],
    )
    assert candidate.sessions == [] and candidate.start_date == day
    assert (
        candidate.summary == "walk" and candidate.location == "park" and candidate.notes == "easy"
    )
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.name == "Alexey"))
        assert alexey
        source = await _message(session, alexey.id, 814, "walk")
        plan = await save_plan(
            session,
            athlete_id=alexey.id,
            candidate=candidate,
            source_message_id=source.id,
            evidence=["walk"],
            today=day,
        )
        parameter = plan.content["blocks"][0]["parameters"][0]
        assert parameter["name"] == "duration_minutes" and parameter["value"] == 20
