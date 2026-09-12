"""Opt-in paid model regression checks; never connects to Telegram or production data."""

import asyncio
import json

from app.ai import AIService
from app.config import get_settings
from app.records import validate_workout

CASES = [
    (
        "banter",
        "не-не. теперь не отвертишься. с тебя теперь 6с :))",
        "Andrey",
        "[speaker=Alexey] Хочу попробовать 6b\n[speaker=Andrey] Леха, ну ты даешь",
        False,
    ),
    ("third_person", "Андрюх, у Сэма опять проблема, потом разберемся", "Alexey", "", False),
    (
        "brief_report",
        "Лазил сегодня, болдеринг легкие трассы, минут 40 после работы",
        "Alexey",
        "",
        False,
    ),
    (
        "fragment",
        "А 5ки 3 шт пролез по 17 метров. Руки в муку!",
        "Andrey",
        (
            'EXISTING SESSIONS: [{"id":42,"date":"2026-09-12","notes":"6а попробовал - и близко не получилось"}]\n'
            "[message=60 speaker=Andrey 2026-09-12T16:00:00] Леха, 6а попробовал - и близко не получилось:)))"
        ),
        False,
    ),
    (
        "followup",
        "Нормально, запас остался, боли нет",
        "Alexey",
        (
            'EXISTING SESSIONS: [{"id":43,"date":"2026-09-12","duration_minutes":40}]\n'
            "PENDING QUESTION workout_id=43: Как ощущения после лазания?"
        ),
        False,
    ),
    (
        "shared_trip",
        "Сэм, наша поездка с Андреем в конце ноября, точной даты нет и она может сдвинуться. Гид будет; лидировать будем отдельные участки.",
        "Alexey",
        "",
        True,
    ),
]


async def main():
    ai = AIService(get_settings())

    async def check(case):
        name, text, athlete, context, addressed = case
        x = await ai.extract(
            text=text,
            athlete_name=athlete,
            local_date="2026-09-12",
            timezone_name="America/Toronto",
            recent_context=context,
            directly_addressed=addressed,
        )
        w = validate_workout(x.workout, text)
        if name in {"banter", "third_person"}:
            passed = (
                not x.should_respond
                and not w.present
                and not x.profile_facts
                and not x.memory.create
            )
        elif name == "brief_report":
            passed = (
                w.present and w.duration_minutes == 40 and all(e.count is None for e in w.climbing)
            )
        elif name == "fragment":
            passed = (
                w.present
                and w.session_action in {"append", "auto"}
                and any(e.count == 3 and e.route_length_m == 17 for e in w.climbing)
            )
        elif name == "followup":
            passed = (
                x.answers_follow_up
                and w.present
                and w.target_workout_id == 43
                and w.pain_status == "reported_none"
            )
        else:
            passed = (
                x.event.shared
                and x.event.date is None
                and x.event.date_precision in {"approximate", "unknown", "month"}
            )
        print(
            json.dumps(
                {
                    "case": name,
                    "passed": passed,
                    "output": x.model_dump(mode="json", exclude_defaults=True),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return passed

    results = await asyncio.gather(*(check(case) for case in CASES))
    reply = await ai.coach_reply(
        context="TODAY 2026-09-12\nAthlete Alexey. Goal: endurance for selected guided outdoor 6a+ pitches, not full-route leadership. No gym partner. 2-3 gym sessions/week, bouldering/autobelays. Trip late November, flexible. Confirmed workout #43 today:40 minutes easy bouldering; grades/count/pain/readiness unknown. User: Сэм, запиши тренировку и подскажи следующий шаг. Ask one useful missing-detail question; no invented quantities.",
        safety_identifier="sam-regression",
    )
    print(
        json.dumps(
            {
                "case": "structured_coach",
                "output": reply.model_dump(mode="json", exclude_defaults=True),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    await ai.client.close()
    if not all(results):
        raise SystemExit("Model regression failed")


if __name__ == "__main__":
    asyncio.run(main())
