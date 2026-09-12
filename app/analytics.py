from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import AnalyticsSnapshot, Workout
from app.grades import grade_rank

__all__ = ["analytics_windows", "grade_rank", "refresh_analytics_snapshots"]


def _metrics(workouts: list[Workout]) -> dict[str, Any]:
    grade_distribution: dict[str, int] = {}
    discipline_counts: dict[str, int] = {}
    rpes: list[float] = []
    pumps: list[float] = []
    loads: list[float] = []
    max_grades: dict[str, tuple[float, str]] = {}
    attempted_grades: dict[str, tuple[float, str]] = {}
    vertical_metres = 0.0
    length_known = 0
    count_unknown = 0
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
            count = entry.count or 0
            count_unknown += entry.count is None
            total_climbs += count
            if entry.completed_count is not None:
                completed_climbs += entry.completed_count
                if entry.route_length_m is not None:
                    vertical_metres += entry.route_length_m * entry.completed_count
                    length_known += entry.completed_count
            discipline = entry.discipline or "unknown"
            discipline_counts[discipline] = discipline_counts.get(discipline, 0) + count
            if entry.original_grade:
                grade_distribution[entry.original_grade] = (
                    grade_distribution.get(entry.original_grade, 0) + count
                )
                rank = grade_rank(entry.grade_system, entry.original_grade)
                if rank is not None:
                    key = f"{discipline}/{entry.grade_system}"
                    current = attempted_grades.get(key)
                    if current is None or rank > current[0]:
                        attempted_grades[key] = (rank, entry.original_grade)
                    if entry.completed_count and entry.completed_count > 0:
                        current = max_grades.get(key)
                        if current is None or rank > current[0]:
                            max_grades[key] = (rank, entry.original_grade)
    duration_known = [w.duration_minutes for w in workouts if w.duration_minutes is not None]
    return {
        "sessions": len(workouts),
        "duration_minutes": sum(duration_known) if duration_known else None,
        "duration_known_sessions": len(duration_known),
        "duration_unknown_sessions": len(workouts) - len(duration_known),
        "unknown_count_entries": count_unknown,
        "total_climbs": total_climbs,
        "completed_climbs": completed_climbs,
        "completed_vertical_metres": vertical_metres if length_known else None,
        "length_known_completed_climbs": length_known,
        "discipline_counts": discipline_counts,
        "grade_distribution": grade_distribution,
        "max_grade_by_discipline": {key: value[1] for key, value in max_grades.items()},
        "max_attempted_grade_by_discipline": {
            key: value[1] for key, value in attempted_grades.items()
        },
        "average_rpe": round(sum(rpes) / len(rpes), 2) if rpes else None,
        "average_pump": round(sum(pumps) / len(pumps), 2) if pumps else None,
        "session_load": round(sum(loads), 1) if loads else None,
        "load_known_sessions": len(loads),
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
            "duration_unknown_sessions": previous_metrics["duration_unknown_sessions"],
            "completed_climbs": previous_metrics["completed_climbs"],
            "unknown_count_entries": previous_metrics["unknown_count_entries"],
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
