"""Catalog, scoped planning, private invitations and durable session delivery.

Revision ID: 20260924_0004
Revises: 20260912_0003
"""

import sqlalchemy as sa
from alembic import op

revision = "20260924_0004"
down_revision = "20260912_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("plans") as batch:
        batch.add_column(sa.Column("scope", sa.String(16), nullable=False, server_default="week"))
        batch.add_column(sa.Column("parent_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("supersedes_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_plans_parent", "plans", ["parent_id"], ["id"])
        batch.create_foreign_key("fk_plans_supersedes", "plans", ["supersedes_id"], ["id"])
        batch.create_index("ix_plans_scope", ["scope"])
    with op.batch_alter_table("workouts") as batch:
        batch.add_column(sa.Column("plan_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_workouts_plan", "plans", ["plan_id"], ["id"])
        batch.create_index("ix_workouts_plan_id", ["plan_id"])
    with op.batch_alter_table("outbox") as batch:
        batch.alter_column("source_message_id", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("reply_to_message_id", existing_type=sa.BigInteger(), nullable=True)
        batch.add_column(sa.Column("dedupe_key", sa.String(160), nullable=True))
        batch.add_column(sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"))
        batch.create_unique_constraint("uq_outbox_dedupe_key", ["dedupe_key"])
    op.create_table(
        "training_preferences",
        sa.Column("athlete_id", sa.Integer(), sa.ForeignKey("athletes.id"), primary_key=True),
        sa.Column("profile", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("private_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("shared_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("morning_time", sa.String(5), nullable=True),
        sa.Column("catalog_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("log_plan_id", sa.Integer(), sa.ForeignKey("plans.id"), nullable=True),
        sa.Column("log_workout_id", sa.Integer(), sa.ForeignKey("workouts.id"), nullable=True),
        sa.Column("log_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "invitations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("athletes.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redeemed_by", sa.BigInteger(), nullable=True),
    )
    op.create_table(
        "catalog_protocols",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("specification", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("slug", "version", name="uq_catalog_protocol_version"),
    )
    for field in ("slug", "category", "status"):
        op.create_index(f"ix_catalog_protocols_{field}", "catalog_protocols", [field])
    op.create_table(
        "catalog_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("athlete_id", sa.Integer(), sa.ForeignKey("athletes.id"), nullable=False),
        sa.Column(
            "source_message_id",
            sa.Integer(),
            sa.ForeignKey("messages.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("file_unique_id", sa.String(200), nullable=False),
        sa.Column("album_id", sa.String(100), nullable=True),
        sa.Column(
            "protocol_id", sa.Integer(), sa.ForeignKey("catalog_protocols.id"), nullable=True
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="received"),
        sa.Column("extracted", sa.JSON(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("athlete_id", "file_unique_id", name="uq_catalog_source_file"),
    )
    op.create_index("ix_catalog_sources_athlete_id", "catalog_sources", ["athlete_id"])


def downgrade() -> None:
    # Downgrade explicitly discards new feature data; back up before invoking it.
    op.drop_table("catalog_sources")
    op.drop_table("catalog_protocols")
    op.drop_table("invitations")
    op.drop_table("training_preferences")
    with op.batch_alter_table("outbox") as batch:
        batch.drop_constraint("uq_outbox_dedupe_key", type_="unique")
        batch.drop_column("dedupe_key")
        batch.drop_column("payload")
    # Nullable delivery sources survive downgrade to preserve scheduled-message history.
    with op.batch_alter_table("workouts") as batch:
        batch.drop_index("ix_workouts_plan_id")
        batch.drop_constraint("fk_workouts_plan", type_="foreignkey")
        batch.drop_column("plan_id")
    with op.batch_alter_table("plans") as batch:
        batch.drop_index("ix_plans_scope")
        batch.drop_constraint("fk_plans_parent", type_="foreignkey")
        batch.drop_constraint("fk_plans_supersedes", type_="foreignkey")
        batch.drop_column("parent_id")
        batch.drop_column("supersedes_id")
        batch.drop_column("scope")
