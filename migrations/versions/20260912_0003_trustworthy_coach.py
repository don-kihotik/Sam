"""Session continuity, visibility and durable delivery.

Revision ID: 20260912_0003
Revises: 20260901_0002
"""

import sqlalchemy as sa
from alembic import op

revision = "20260912_0003"
down_revision = "20260901_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages", sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
    )
    op.alter_column(
        "workout_entries", "count", existing_type=sa.Integer(), nullable=True, server_default=None
    )
    op.add_column("workout_entries", sa.Column("route_length_m", sa.Float(), nullable=True))
    for table in ("facts", "memories", "plans", "events"):
        op.add_column(
            table, sa.Column("visibility", sa.String(16), nullable=False, server_default="private")
        )
    op.add_column(
        "facts", sa.Column("status", sa.String(16), nullable=False, server_default="active")
    )
    op.add_column("events", sa.Column("shared_key", sa.String(80), nullable=True))
    op.create_index("ix_events_shared_key", "events", ["shared_key"])
    op.add_column(
        "processing_runs", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("processing_runs", sa.Column("next_attempt_at", sa.DateTime(timezone=True)))
    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("athlete_id", sa.Integer(), sa.ForeignKey("athletes.id"), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("reply_to_message_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("sent_message_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("source_message_id", "revision", name="uq_outbox_message_revision"),
    )
    op.create_index("ix_outbox_source_message_id", "outbox", ["source_message_id"])
    op.create_index("ix_outbox_status", "outbox", ["status"])
    op.create_table(
        "follow_ups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("athlete_id", sa.Integer(), sa.ForeignKey("athletes.id"), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("workout_id", sa.Integer(), sa.ForeignKey("workouts.id")),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_follow_ups_athlete_id", "follow_ups", ["athlete_id"])


def downgrade() -> None:
    op.drop_table("follow_ups")
    op.drop_table("outbox")
    op.drop_column("processing_runs", "next_attempt_at")
    op.drop_column("processing_runs", "attempts")
    op.drop_index("ix_events_shared_key", table_name="events")
    op.drop_column("events", "shared_key")
    op.drop_column("facts", "status")
    for table in ("facts", "memories", "plans", "events"):
        op.drop_column(table, "visibility")
    op.drop_column("workout_entries", "route_length_m")
    # Nullable counts deliberately survive rollback: unknown must not become invented one.
    op.drop_column("messages", "revision")
