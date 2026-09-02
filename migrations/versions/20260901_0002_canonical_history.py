"""Add canonical workout history and analytics.

Revision ID: 20260901_0002
Revises: 20260830_0001
"""

import sqlalchemy as sa
from alembic import op

revision = "20260901_0002"
down_revision = "20260830_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("workouts", "date", existing_type=sa.Date(), nullable=True)
    op.add_column(
        "workouts",
        sa.Column("date_precision", sa.String(20), nullable=False, server_default="exact"),
    )
    op.add_column(
        "workouts",
        sa.Column("pain_status", sa.String(24), nullable=False, server_default="unknown"),
    )
    op.add_column("workouts", sa.Column("evidence", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("workouts", sa.Column("fingerprint", sa.String(64), nullable=True))
    op.create_index("ix_workouts_fingerprint", "workouts", ["fingerprint"])

    op.create_table(
        "workout_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workout_id",
            sa.Integer(),
            sa.ForeignKey("workouts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("discipline", sa.String(40), nullable=True),
        sa.Column("grade_system", sa.String(20), nullable=True),
        sa.Column("original_grade", sa.String(30), nullable=True),
        sa.Column("grade_rank", sa.Float(), nullable=True),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("completed_count", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=True),
        sa.Column("wall_style", sa.String(80), nullable=True),
        sa.Column("movement_style", sa.String(80), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_workout_entries_workout_id", "workout_entries", ["workout_id"])
    op.create_index("ix_workout_entries_discipline", "workout_entries", ["discipline"])

    op.add_column("plans", sa.Column("evidence", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column(
        "events", sa.Column("date_precision", sa.String(20), nullable=False, server_default="exact")
    )
    op.add_column("events", sa.Column("evidence", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column(
        "events", sa.Column("source_message_ids", sa.JSON(), nullable=False, server_default="[]")
    )
    op.add_column(
        "events",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "analytics_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("athlete_id", sa.Integer(), sa.ForeignKey("athletes.id"), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "athlete_id", "period_end", "window_days", name="uq_analytics_snapshot_period"
        ),
    )
    op.create_index("ix_analytics_snapshots_athlete_id", "analytics_snapshots", ["athlete_id"])
    op.create_index("ix_analytics_snapshots_period_end", "analytics_snapshots", ["period_end"])


def downgrade() -> None:
    op.drop_index("ix_analytics_snapshots_period_end", table_name="analytics_snapshots")
    op.drop_index("ix_analytics_snapshots_athlete_id", table_name="analytics_snapshots")
    op.drop_table("analytics_snapshots")
    for column in ("updated_at", "source_message_ids", "evidence", "date_precision"):
        op.drop_column("events", column)
    op.drop_column("plans", "evidence")
    op.drop_index("ix_workout_entries_discipline", table_name="workout_entries")
    op.drop_index("ix_workout_entries_workout_id", table_name="workout_entries")
    op.drop_table("workout_entries")
    op.drop_index("ix_workouts_fingerprint", table_name="workouts")
    for column in ("fingerprint", "evidence", "pain_status", "date_precision"):
        op.drop_column("workouts", column)
    op.alter_column("workouts", "date", existing_type=sa.Date(), nullable=False)
