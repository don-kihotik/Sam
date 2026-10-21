"""Opt-in real-model training extraction check; synthetic data, no DB or Telegram writes.

Run: python -m app.evaluate_training
Uses the configured extraction model/API key. Prints only test names and outcomes.
"""

import argparse
import asyncio
import json

from app.ai import AIService
from app.config import get_settings
from app.records import validate_workout


async def main(case: str | None = None) -> None:
    settings = get_settings()
    if not settings.openai_ready:
        raise SystemExit("OpenAI credentials are not configured")
    ai = AIService(settings)
    cases = [
        ("actual_strength", "Сегодня жим лёжа: 40 кг, три подхода — 8/8/6 повторов.", ""),
        ("actual_run_units", "Сегодня пробежал 5 км за 30 минут.", ""),
        (
            "completion_is_not_numbers",
            "Готово.",
            "OPEN LOG: plan_id=7. Plan prescribed bench press 40kg, 3x8. No actual report yet.",
        ),
        (
            "catalog_is_not_workout",
            "Это для каталога, я не делал: висы 6 подходов по 10 секунд.",
            "",
        ),
    ]
    failed = []
    try:
        for name, text, context in cases:
            if case and name != case:
                continue
            try:
                extraction = await ai.extract(
                    text=text,
                    athlete_name="Synthetic Test Athlete",
                    local_date="2026-09-24",
                    timezone_name="America/Toronto",
                    recent_context=context,
                    directly_addressed=True,
                )
                workout = validate_workout(extraction.workout, text)
                if name == "actual_strength":
                    sets = [actual for activity in workout.activities for actual in activity.sets]
                    passed = (
                        workout.present
                        and [item.reps for item in sets] == [8, 8, 6]
                        and all(item.weight_kg == 40 for item in sets)
                    )
                elif name == "actual_run_units":
                    passed = workout.present and any(
                        activity.distance_m == 5000
                        and (activity.duration_minutes or workout.duration_minutes) == 30
                        for activity in workout.activities
                    )
                elif name == "completion_is_not_numbers":
                    passed = (
                        not workout.confirmed_planned_values
                        and not workout.climbing
                        and not any(
                            activity.sets or activity.distance_m is not None
                            for activity in workout.activities
                        )
                    )
                else:
                    passed = not workout.present
                print(f"{name}: {'PASS' if passed else 'FAIL'}", flush=True)
                if not passed:
                    failed.append(name)
                    print(
                        json.dumps(
                            {
                                "synthetic_candidate": extraction.workout.model_dump(mode="json"),
                                "validated": workout.model_dump(mode="json"),
                            },
                            ensure_ascii=True,
                        ),
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001 - classify provider errors without printing secrets
                print(f"{name}: ERROR ({type(exc).__name__})", flush=True)
                failed.append(name)
        if case in (None, "session_plan"):
            name = "session_plan"
            try:
                result = await ai.coach_reply(
                    context=(
                        "TODAY: 2026-09-24. Private conversation with synthetic athlete. "
                        "CURRENT USER MESSAGE: Составь и сохрани занятие на сегодня: "
                        "20 минут спокойной прогулки и 5 минут лёгкой мобилити дома. "
                        "Боли нет, чувствую себя хорошо. "
                        "No existing program/week/session. No catalog protocols available. "
                        "This is a future plan, not a performed workout."
                    ),
                    safety_identifier="synthetic-training-evaluation",
                )
                plan = result.plan
                passed = (
                    plan.action == "create"
                    and plan.scope == "session"
                    and len(plan.blocks) >= 2
                    and all(block.ad_hoc and block.protocol_id is None for block in plan.blocks)
                )
                print(f"{name}: {'PASS' if passed else 'FAIL'}", flush=True)
                if not passed:
                    failed.append(name)
                    print(json.dumps(plan.model_dump(mode="json"), ensure_ascii=True), flush=True)
            except Exception as exc:  # noqa: BLE001 - provider error classification only
                print(f"{name}: ERROR ({type(exc).__name__})", flush=True)
                failed.append(name)
    finally:
        await ai.client.close()
    if failed:
        raise SystemExit(f"Failed {len(failed)} cases")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        choices=[
            "actual_strength",
            "actual_run_units",
            "completion_is_not_numbers",
            "catalog_is_not_workout",
            "session_plan",
        ],
    )
    asyncio.run(main(parser.parse_args().case))
