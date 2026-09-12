from datetime import date as DateType
from typing import Literal

from pydantic import BaseModel, Field


class ClimbingEntry(BaseModel):
    discipline: str | None = None
    grade_system: str | None = None
    original_grade: str | None = None
    count: int | None = Field(default=None, ge=1, le=1000)
    route_length_m: float | None = Field(default=None, gt=0, le=1000)
    evidence: list[str] = Field(default_factory=list)
    completed: bool | None = None
    attempts: int | None = Field(default=None, ge=1, le=100)
    wall_style: str | None = None
    movement_style: str | None = None
    notes: str | None = None


class NamedScore(BaseModel):
    area: str
    value: float = Field(ge=0, le=10)


class WorkoutCandidate(BaseModel):
    present: bool = False
    session_action: Literal["auto", "new", "append", "correct"] = "auto"
    target_workout_id: int | None = None
    date: DateType | None = None
    date_inferred: bool = False
    date_precision: Literal["exact", "day", "month", "unknown"] = "exact"
    workout_type: str | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    rpe: float | None = Field(default=None, ge=0, le=10)
    pump: float | None = Field(default=None, ge=0, le=10)
    pain: list[NamedScore] = Field(default_factory=list)
    pain_status: Literal["unknown", "reported_none", "reported"] = "unknown"
    climbing: list[ClimbingEntry] = Field(default_factory=list)
    notes: str | None = None
    confidence: float = Field(default=1, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)


class DailyStateCandidate(BaseModel):
    present: bool = False
    date: DateType | None = None
    energy: float | None = Field(default=None, ge=1, le=10)
    sleep_duration_hours: float | None = Field(default=None, ge=0, le=24)
    sleep_quality: float | None = Field(default=None, ge=1, le=10)
    motivation: float | None = Field(default=None, ge=1, le=10)
    stress: float | None = Field(default=None, ge=0, le=10)
    general_fatigue: float | None = Field(default=None, ge=0, le=10)
    soreness: list[NamedScore] = Field(default_factory=list)
    available_time_minutes: int | None = Field(default=None, ge=0, le=1440)
    notes: str | None = None
    confidence: float = Field(default=1, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)


class CorrectionCandidate(BaseModel):
    target_kind: Literal["workout", "daily_state", "fact", "athlete", "memory", "plan", "event"]
    target_id: int | None = None
    evidence: list[str] = Field(default_factory=list)
    target_date: DateType | None = None
    field: str | None = None
    old_value: str | float | int | bool | None = None
    new_value: str | float | int | bool | None = None
    correct_athlete_name: Literal["Alexey", "Andrey"] | None = None
    negate: bool = False
    confidence: float = Field(default=1, ge=0, le=1)


class BacklogCandidate(BaseModel):
    action: Literal["none", "create", "confirm"] = "none"
    item_type: Literal["bug", "feature", "improvement", "idea", "technical_debt"] | None = None
    title: str | None = None
    description: str | None = None


class MemoryCandidate(BaseModel):
    create: bool = False
    kind: Literal["episodic", "coach"] = "episodic"
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=0.7, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    supersedes_id: int | None = None


class EventCandidate(BaseModel):
    action: Literal["none", "create", "update"] = "none"
    name: str | None = None
    date: DateType | None = None
    date_precision: Literal["exact", "day", "month", "approximate", "unknown"] = "unknown"
    date_label: str | None = None
    route_type: str | None = None
    guided: bool | None = None
    shared: bool = False
    evidence: list[str] = Field(default_factory=list)


class WeeklyScheduleItem(BaseModel):
    day: str
    activity: str


class PlanCandidate(BaseModel):
    action: Literal["none", "create", "update"] = "none"
    start_date: DateType | None = None
    end_date: DateType | None = None
    summary: str | None = None
    focus: list[str] = Field(default_factory=list)
    # Structured Outputs does not support arbitrary-key dictionaries.  A list of
    # fixed-shape objects produces a strict JSON schema accepted by the API.
    weekly_schedule: list[WeeklyScheduleItem] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class CoachArtifacts(BaseModel):
    plan: PlanCandidate = Field(default_factory=PlanCandidate)


class ProfileFactCandidate(BaseModel):
    field: str
    value: str
    evidence: list[str] = Field(default_factory=list)


class CoachReply(BaseModel):
    text: str
    plan: PlanCandidate = Field(default_factory=PlanCandidate)
    follow_up_question: str | None = None
    follow_up_workout_id: int | None = None


class MessageExtraction(BaseModel):
    normalized_text: str
    should_respond: bool = False
    conversation_intent: Literal[
        "silent", "addressed", "continuation", "training_report", "important_risk", "clarification"
    ] = "silent"
    answers_follow_up: bool = False
    is_banter: bool = False
    response_reason: str | None = None
    workout: WorkoutCandidate = Field(default_factory=WorkoutCandidate)
    additional_workouts: list[WorkoutCandidate] = Field(default_factory=list)
    profile_facts: list[ProfileFactCandidate] = Field(default_factory=list)
    daily_state: DailyStateCandidate = Field(default_factory=DailyStateCandidate)
    weight_kg: float | None = Field(default=None, ge=20, le=350)
    weight_evidence: list[str] = Field(default_factory=list)
    corrections: list[CorrectionCandidate] = Field(default_factory=list)
    backlog: BacklogCandidate = Field(default_factory=BacklogCandidate)
    memory: MemoryCandidate = Field(default_factory=MemoryCandidate)
    event: EventCandidate = Field(default_factory=EventCandidate)
    plan: PlanCandidate = Field(default_factory=PlanCandidate)
    uncertainty_notes: list[str] = Field(default_factory=list)
