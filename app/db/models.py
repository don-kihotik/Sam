from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Athlete(Base):
    __tablename__ = "athletes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    timezone: Mapped[str] = mapped_column(String(80), default="America/Toronto")
    profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("telegram_chat_id", "telegram_message_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    athlete_id: Mapped[int | None] = mapped_column(ForeignKey("athletes.id"), index=True)
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    direction: Mapped[str] = mapped_column(String(16), default="incoming")
    message_type: Mapped[str] = mapped_column(String(24), default="text")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_data: Mapped[dict[str, Any]] = mapped_column(
        "attachment_metadata", JSON, default=dict
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    processing_data: Mapped[dict[str, Any]] = mapped_column(
        "processing_metadata", JSON, default=dict
    )
    telegram_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    athlete: Mapped[Athlete | None] = relationship()


class Workout(Base):
    __tablename__ = "workouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), nullable=True, index=True)
    date: Mapped[date | None] = mapped_column(Date, index=True, nullable=True)
    date_precision: Mapped[str] = mapped_column(String(20), default="exact")
    type: Mapped[str] = mapped_column(String(40))
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    rpe: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    structured_details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    pain_status: Mapped[str] = mapped_column(String(24), default="unknown")
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_message_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    entries: Mapped[list[WorkoutEntry]] = relationship(
        back_populates="workout", cascade="all, delete-orphan", lazy="selectin"
    )


class WorkoutEntry(Base):
    __tablename__ = "workout_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    workout_id: Mapped[int] = mapped_column(
        ForeignKey("workouts.id", ondelete="CASCADE"), index=True
    )
    discipline: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    grade_system: Mapped[str | None] = mapped_column(String(20), nullable=True)
    original_grade: Mapped[str | None] = mapped_column(String(30), nullable=True)
    grade_rank: Mapped[float | None] = mapped_column(Float, nullable=True)
    count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    route_length_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    completed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wall_style: Mapped[str | None] = mapped_column(String(80), nullable=True)
    movement_style: Mapped[str | None] = mapped_column(String(80), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    workout: Mapped[Workout] = relationship(back_populates="entries")


class DailyState(Base):
    __tablename__ = "daily_states"
    __table_args__ = (UniqueConstraint("athlete_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    values: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_message_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Fact(Base):
    __tablename__ = "facts"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    field: Mapped[str] = mapped_column(String(100), index=True)
    value: Mapped[Any] = mapped_column(JSON)
    source_message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))
    inferred: Mapped[bool] = mapped_column(Boolean, default=False)
    visibility: Mapped[str] = mapped_column(String(16), default="private")
    status: Mapped[str] = mapped_column(String(16), default="active")
    confidence: Mapped[float] = mapped_column(Float, default=1)
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("facts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BodyMeasurement(Base):
    __tablename__ = "body_measurements"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    weight_kg: Mapped[float] = mapped_column(Float)
    source_message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(20))
    visibility: Mapped[str] = mapped_column(String(16), default="private")
    summary: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_message_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    confidence: Mapped[float] = mapped_column(Float, default=0.7)
    status: Mapped[str] = mapped_column(String(20), default="hypothesis")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    scope: Mapped[str] = mapped_column(String(16), default="week", index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), nullable=True)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    visibility: Mapped[str] = mapped_column(String(16), default="private")
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_message_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    visibility: Mapped[str] = mapped_column(String(16), default="private")
    shared_key: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    date: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_precision: Mapped[str] = mapped_column(String(20), default="exact")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_message_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AnalyticsSnapshot(Base):
    __tablename__ = "analytics_snapshots"
    __table_args__ = (UniqueConstraint("athlete_id", "period_end", "window_days"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    period_end: Mapped[date] = mapped_column(Date, index=True)
    window_days: Mapped[int] = mapped_column(Integer)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class BacklogItem(Base):
    __tablename__ = "backlog_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("athletes.id"))
    type: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str | None] = mapped_column(Text)
    original_message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="new")
    priority: Mapped[str | None] = mapped_column(String(20), nullable=True)
    github_issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    github_issue_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Correction(Base):
    __tablename__ = "corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"))
    source_message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))
    target_kind: Mapped[str] = mapped_column(String(30))
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProcessingRun(Base):
    __tablename__ = "processing_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="started")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extraction_model: Mapped[str | None] = mapped_column(String(100))
    coaching_model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(40), default="v1")
    extracted: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    mutations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (UniqueConstraint("source_message_id", "revision"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id"), index=True, nullable=True
    )
    dedupe_key: Mapped[str | None] = mapped_column(String(160), unique=True, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"))
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger)
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    sent_message_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FollowUp(Base):
    __tablename__ = "follow_ups"

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))
    workout_id: Mapped[int | None] = mapped_column(ForeignKey("workouts.id"), nullable=True)
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrainingPreference(Base):
    __tablename__ = "training_preferences"

    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), primary_key=True)
    profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    private_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    shared_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    morning_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    catalog_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    log_plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), nullable=True)
    log_workout_id: Mapped[int | None] = mapped_column(ForeignKey("workouts.id"), nullable=True)
    log_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("athletes.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    redeemed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class CatalogProtocol(Base):
    __tablename__ = "catalog_protocols"
    __table_args__ = (UniqueConstraint("slug", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(240))
    category: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    specification: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CatalogSource(Base):
    __tablename__ = "catalog_sources"
    __table_args__ = (UniqueConstraint("athlete_id", "file_unique_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    source_message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), unique=True)
    file_unique_id: Mapped[str] = mapped_column(String(200))
    album_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    protocol_id: Mapped[int | None] = mapped_column(
        ForeignKey("catalog_protocols.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), default="received")
    extracted: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
