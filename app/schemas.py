from datetime import date as DateType
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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


class ActualSetCandidate(BaseModel):
    """One actually performed set; prescriptions are never copied here."""

    set_number: int | None = Field(default=None, ge=1, le=100)
    reps: float | None = Field(default=None, gt=0, le=10000)
    weight_kg: float | None = Field(default=None, ge=0, le=1000)
    duration_seconds: float | None = Field(default=None, gt=0, le=86400)
    distance_m: float | None = Field(default=None, gt=0, le=2_000_000)
    completed: bool | None = None
    evidence: list[str] = Field(default_factory=list)


class ActualActivityCandidate(BaseModel):
    kind: Literal["strength", "run", "climb", "mobility", "other"] = "other"
    title: str | None = None
    exercise: str | None = None
    # E.g. grip, surface, route or weather.  Analytics never combines unlike conditions.
    condition: str | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    distance_m: float | None = Field(default=None, gt=0, le=2_000_000)
    sets: list[ActualSetCandidate] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class WorkoutCandidate(BaseModel):
    present: bool = False
    session_action: Literal["auto", "new", "append", "correct"] = "auto"
    target_workout_id: int | None = None
    date: DateType | None = None
    date_inferred: bool = False
    date_precision: Literal["exact", "day", "month", "unknown"] = "exact"
    workout_type: str | None = None
    plan_id: int | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    rpe: float | None = Field(default=None, ge=0, le=10)
    pump: float | None = Field(default=None, ge=0, le=10)
    pain: list[NamedScore] = Field(default_factory=list)
    pain_status: Literal["unknown", "reported_none", "reported"] = "unknown"
    climbing: list[ClimbingEntry] = Field(default_factory=list)
    activities: list[ActualActivityCandidate] = Field(default_factory=list)
    # Must be set only by a confirmation flow which shows the exact values.
    # A bare completion never fills otherwise unknown actual values.
    confirmed_planned_values: bool = False
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
    correct_athlete_name: str | None = None
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


class PlanParameter(BaseModel):
    """Fixed-shape scalar plan parameter for strict structured outputs."""

    name: str
    value: str | float | int | bool | None = None
    unit: str | None = None
    target: str | None = None


class PlannedBlockCandidate(BaseModel):
    kind: str
    protocol_id: int | None = None
    protocol_version: int | None = Field(default=None, ge=1)
    title: str | None = None
    parameters: list[PlanParameter] = Field(default_factory=list)
    instruction: str | None = None
    adaptation_reason: str | None = None
    ad_hoc: bool = False
    source: str | None = None


class PlannedSessionCandidate(BaseModel):
    date: DateType | None = None
    title: str | None = None
    location: str | None = None
    blocks: list[PlannedBlockCandidate] = Field(default_factory=list)
    notes: str | None = None


class PlanGoalCandidate(BaseModel):
    title: str
    target_date: DateType | None = None
    metric: str | None = None
    target_value: str | float | int | None = None


class PlanPhaseCandidate(BaseModel):
    title: str
    start_date: DateType | None = None
    end_date: DateType | None = None
    objective: str | None = None


class PlanCandidate(BaseModel):
    action: Literal["none", "create", "update"] = "none"
    scope: Literal["program", "week", "session"] = "week"
    target_plan_id: int | None = None
    parent_plan_id: int | None = None
    location: str | None = None
    start_date: DateType | None = None
    end_date: DateType | None = None
    title: str | None = None
    summary: str | None = None
    notes: str | None = None
    focus: list[str] = Field(default_factory=list)
    # Structured Outputs does not support arbitrary-key dictionaries.  A list of
    # fixed-shape objects produces a strict JSON schema accepted by the API.
    weekly_schedule: list[WeeklyScheduleItem] = Field(default_factory=list)
    goals: list[PlanGoalCandidate] = Field(default_factory=list)
    phases: list[PlanPhaseCandidate] = Field(default_factory=list)
    sessions: list[PlannedSessionCandidate] = Field(default_factory=list)
    blocks: list[PlannedBlockCandidate] = Field(default_factory=list)
    adaptation_reason: str | None = None
    guardrails: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def promote_single_nested_session(self) -> "PlanCandidate":
        """Accept one common model shape without silently dropping its blocks."""

        if self.action == "none":
            # Structured outputs still fill optional container fields for a
            # no-op. They are not a prescription and must never be persisted.
            self.blocks = []
            self.sessions = []
            return self
        if self.scope != "session" or not self.sessions:
            return self
        if len(self.sessions) != 1 or self.blocks or self.weekly_schedule:
            raise ValueError("a session plan cannot mix outer and nested session prescriptions")
        nested = self.sessions[0]
        if (
            self.start_date is not None
            and nested.date is not None
            and self.start_date != nested.date
        ):
            raise ValueError("nested session date conflicts with session plan date")
        if self.end_date is not None and nested.date is not None and self.end_date != nested.date:
            raise ValueError("nested session date conflicts with session plan end date")
        if self.location and nested.location and self.location != nested.location:
            raise ValueError("nested session location conflicts with session plan location")
        self.start_date = self.start_date or nested.date
        self.end_date = self.end_date or nested.date
        self.location = self.location or nested.location
        self.title = self.title or nested.title
        self.summary = self.summary or nested.title
        self.notes = self.notes or nested.notes
        self.blocks = nested.blocks
        self.sessions = []
        return self


class CoachArtifacts(BaseModel):
    plan: PlanCandidate = Field(default_factory=PlanCandidate)


class ProfileFactCandidate(BaseModel):
    field: str
    value: str
    evidence: list[str] = Field(default_factory=list)


class ProfileGoalCandidate(BaseModel):
    title: str
    target_date: DateType | None = None
    metric: str | None = None
    target_value: str | float | int | None = None
    evidence: list[str] = Field(default_factory=list)


class AvailabilityCandidate(BaseModel):
    day: str | None = None
    minutes: int | None = Field(default=None, ge=0, le=1440)
    evidence: list[str] = Field(default_factory=list)


class EquipmentCandidate(BaseModel):
    name: str
    available: bool | None = None
    location: str | None = None
    evidence: list[str] = Field(default_factory=list)


class ExperienceCandidate(BaseModel):
    activity: str
    level: str | None = None
    flash_grade: str | None = None
    onsight_grade: str | None = None
    evidence: list[str] = Field(default_factory=list)


class LocationCandidate(BaseModel):
    name: str
    evidence: list[str] = Field(default_factory=list)


class PartnerCandidate(BaseModel):
    name: str | None = None
    availability: str | None = None
    evidence: list[str] = Field(default_factory=list)


class TrainingProfileCandidate(BaseModel):
    """Only explicit training inputs.  Commands own private delivery settings."""

    goals: list[ProfileGoalCandidate] = Field(default_factory=list)
    availability: list[AvailabilityCandidate] = Field(default_factory=list)
    locations: list[LocationCandidate] = Field(default_factory=list)
    equipment: list[EquipmentCandidate] = Field(default_factory=list)
    experience: list[ExperienceCandidate] = Field(default_factory=list)
    partners: list[PartnerCandidate] = Field(default_factory=list)
    # A replacement is only applied by persistence when its own evidence is current.
    replace_fields: list[
        Literal["goals", "availability", "locations", "equipment", "experience", "partners"]
    ] = Field(default_factory=list)
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
    training_profile: TrainingProfileCandidate = Field(default_factory=TrainingProfileCandidate)
    daily_state: DailyStateCandidate = Field(default_factory=DailyStateCandidate)
    weight_kg: float | None = Field(default=None, ge=20, le=350)
    weight_evidence: list[str] = Field(default_factory=list)
    corrections: list[CorrectionCandidate] = Field(default_factory=list)
    backlog: BacklogCandidate = Field(default_factory=BacklogCandidate)
    memory: MemoryCandidate = Field(default_factory=MemoryCandidate)
    event: EventCandidate = Field(default_factory=EventCandidate)
    plan: PlanCandidate = Field(default_factory=PlanCandidate)
    uncertainty_notes: list[str] = Field(default_factory=list)
