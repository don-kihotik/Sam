"""Small, strict schemas for the bounded protocol catalog.

The catalog deliberately describes a source prescription, rather than a user's
completed training.  The structures are JSON-friendly because protocol
snapshots are embedded in immutable plans.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class CatalogParameter(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    value: str | int | float | bool
    unit: str | None = Field(default=None, max_length=40)
    target: str | None = Field(default=None, max_length=160)


class CatalogStep(BaseModel):
    """A typed step; ``children`` retains ordering and nesting."""

    type: Literal["action", "sequence", "repeat", "rest", "side", "choice"]
    label: str | None = None
    sets: int | None = Field(default=None, ge=1)
    reps: int | None = Field(default=None, ge=1)
    seconds: int | None = Field(default=None, ge=1)
    rest_seconds: int | None = Field(default=None, ge=0)
    seconds_min: int | None = Field(default=None, ge=1)
    seconds_max: int | None = Field(default=None, ge=1)
    rest_seconds_min: int | None = Field(default=None, ge=0)
    rest_seconds_max: int | None = Field(default=None, ge=0)
    sides: list[Literal["left", "right"]] | None = None
    instruction: str | None = None
    children: list[CatalogStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def ranges_are_ordered(self) -> CatalogStep:
        if (
            self.seconds_min is not None
            and self.seconds_max is not None
            and self.seconds_min > self.seconds_max
        ):
            raise ValueError("seconds range is inverted")
        if (
            self.rest_seconds_min is not None
            and self.rest_seconds_max is not None
            and self.rest_seconds_min > self.rest_seconds_max
        ):
            raise ValueError("rest range is inverted")
        return self


class AdjustableParameter(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    unit: str | None = Field(default=None, max_length=40)
    minimum: float | None = None
    maximum: float | None = None
    choices: list[str | int | float | bool] = Field(default_factory=list)


class SamAdaptationPolicy(BaseModel):
    """A product policy layered over, never attributed to, the source card."""

    policy: Literal["sam_volume_reduction_v1"]
    provenance: Literal["sam_adaptation_policy_not_source"]
    parameter_name: str
    root_step_field: Literal["sets", "reps"]
    source_count: int = Field(ge=1)
    minimum: int = Field(default=1, ge=1)
    transformation: Literal["reduce_root_repeat_count_only"]


class CatalogSpecification(BaseModel):
    protocol_type: Literal["technique", "conditioning", "climbing", "warmup", "assessment"]
    original_category: str
    original_subcategory: str | None = None
    source_completeness: Literal["complete", "partial", "list_only", "conflicting"]
    unresolved_fields: list[str] = Field(default_factory=list)
    purpose: list[str] = Field(default_factory=list)
    effects: list[dict[str, str]] = Field(default_factory=list)
    load_basis: dict[str, Any] = Field(default_factory=dict)
    duration: dict[str, Any] = Field(default_factory=dict)
    equipment: dict[str, list[str]] = Field(default_factory=dict)
    location: dict[str, Any] = Field(default_factory=dict)
    eligibility: dict[str, Any] = Field(default_factory=dict)
    restrictions: list[dict[str, str]] = Field(default_factory=list)
    steps: list[CatalogStep] = Field(default_factory=list)
    adjustable_parameters: list[AdjustableParameter] = Field(default_factory=list)
    default_parameters: list[CatalogParameter] = Field(default_factory=list)
    sam_adaptation_policy: SamAdaptationPolicy | None = None

    @model_validator(mode="after")
    def complete_ready_specs_have_steps(self) -> CatalogSpecification:
        # The caller additionally enforces status.  This catches malformed
        # ready inventory before it can accidentally be published.
        if self.source_completeness == "complete" and not self.steps:
            raise ValueError("complete source specifications need ordered steps")
        return self


CatalogStep.model_rebuild()
