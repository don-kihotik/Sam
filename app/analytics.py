from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import AnalyticsSnapshot, Workout


def grade_rank(system: str | None, grade: str | None) -> float | None:
    if not grade:
        return None
    value = grade.strip().upper()
    if (system or "").upper() in {"V", "HUECO"} or value.startswith("V"):
        match = re.fullmatch(r"V(\d+)", value)
        return float(match.group(1)) if match else None
    match = re.fullmatch(r"5\.(\d+)([ABCD])?([+-])?", value)
    if not match:
        return None
    rank = float(match.group(1))
    if match.group(2):
        rank += {"A": 0.1, "B": 0.3, "C": 0.6, "D": 0.8}[match.group(2)]
    if match.group(3) == "+":
        rank += 0.05
    elif match.group(3) == "-":
        rank -= 0.05
    return rank


def _metrics(workouts: list[Workout]) -> dict[str, Any]:
    grade_distribution: dict[str, int] = {}
    discipline_counts: dict[str, int] = {}
    rpes: list[float] = []
    pumps: list[float] = []
    loads: list[float] = []
    max_grades: dict[str, tuple[float, str]] = {}
    total_climbs = 0
    completed_climbs = 0
    for workout in workouts:
        if workout.rpe is not None:
            rpes.append(workout.rpe)
            if workout.duration_minutes is not None:
                loads.append(workout.rpe * workout.duration_minutes)
        pump = (workout.structured_details or {}).get("pump")
        if isinstance(pump, (int, float)):
            pumps.append(float(pump))
        for entry in workout.entries:
            count = entry.count or 1
            total_climbs += count
            if entry.completed_count is not None:
                completed_climbs += entry.completed_count
            discipline = entry.discipline or "unknown"
            discipline_counts[discipline] = discipline_counts.get(discipline, 0) + count
            if entry.original_grade:
                grade_distribution[entry.original_grade] = (
                    grade_distribution.get(entry.original_grade, 0) + count
                )
                rank = entry.grade_rank
                if rank is not None:
                    current = max_grades.get(discipline)
                    if current is None or rank > current[0]:
                        max_grades[discipline] = (rank, entry.original_grade)
    return {
        "sessions": len(workouts),
        "duration_minutes": sum(w.duration_minutes or 0 for w in workouts),
        "total_climbs": total_climbs,
        "completed_climbs": completed_climbs,
        "discipline_counts": discipline_counts,
        "grade_distribution": grade_distribution,
        "max_grade_by_discipline": {key: value[1] for key, value in max_grades.items()},
        "average_rpe": round(sum(rpes) / len(rpes), 2) if rpes else None,
        "average_pump": round(sum(pumps) / len(pumps), 2) if pumps else None,
        "session_load": round(sum(loads), 1),
        "pain_reported_sessions": sum(w.pain_status == "reported" for w in workouts),
        "pain_free_sessions": sum(w.pain_status == "reported_none" for w in workouts),
        "pain_unknown_sessions": sum(w.pain_status == "unknown" for w in workouts),
    }


async def analytics_windows(
    session: AsyncSession, athlete_id: int, period_end: date, windows: tuple[int, ...] = (7, 28, 90)
) -> dict[str, Any]:
    earliest = period_end - timedelta(days=max(windows) * 2 - 1)
    rows = (
        await session.scalars(
            select(Workout)
            .options(selectinload(Workout.entries))
            .where(
                Workout.athlete_id == athlete_id,
                Workout.status == "active",
                Workout.date.is_not(None),
                Workout.date >= earliest,
                Workout.date <= period_end,
            )
            .order_by(Workout.date)
        )
    ).all()
    result: dict[str, Any] = {}
    for window in windows:
        start = period_end - timedelta(days=window - 1)
        previous_start = start - timedelta(days=window)
        current = [w for w in rows if w.date is not None and start <= w.date <= period_end]
        previous = [w for w in rows if w.date is not None and previous_start <= w.date < start]
        current_metrics = _metrics(current)
        previous_metrics = _metrics(previous)
        current_metrics["previous"] = {
            "sessions": previous_metrics["sessions"],
            "duration_minutes": previous_metrics["duration_minutes"],
            "total_climbs": previous_metrics["total_climbs"],
            "session_load": previous_metrics["session_load"],
        }
        result[str(window)] = current_metrics
    all_workouts = (
        await session.scalars(
            select(Workout)
            .where(Workout.athlete_id == athlete_id, Workout.status == "active")
            .order_by(Workout.date, Workout.created_at)
        )
    ).all()
    result["all_time"] = {"sessions": len(all_workouts)}
    return result


async def refresh_analytics_snapshots(
    session: AsyncSession, athlete_id: int, period_end: date
) -> dict[str, Any]:
    metrics = await analytics_windows(session, athlete_id, period_end)
    for window in (7, 28, 90):
        snapshot = await session.scalar(
            select(AnalyticsSnapshot).where(
                AnalyticsSnapshot.athlete_id == athlete_id,
                AnalyticsSnapshot.period_end == period_end,
                AnalyticsSnapshot.window_days == window,
            )
        )
        if snapshot is None:
            snapshot = AnalyticsSnapshot(
                athlete_id=athlete_id, period_end=period_end, window_days=window
            )
            session.add(snapshot)
        snapshot.metrics = metrics[str(window)]
    await session.flush()
    return metrics
