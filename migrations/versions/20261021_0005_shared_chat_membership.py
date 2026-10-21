"""Multi-group membership and reply-based invitations.

Revision ID: 20261021_0005
Revises: 20260924_0004
"""

import sqlalchemy as sa
from alembic import op

revision = "20261021_0005"
down_revision = "20260924_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shared_chats",
        sa.Column(
            "telegram_chat_id", sa.BigInteger(), primary_key=True, autoincrement=False
        ),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("athletes.id"), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_shared_chats_created_by", "shared_chats", ["created_by"])
    op.create_table(
        "shared_chat_memberships",
        sa.Column(
            "telegram_chat_id",
            sa.BigInteger(),
            sa.ForeignKey("shared_chats.telegram_chat_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "athlete_id",
            sa.Integer(),
            sa.ForeignKey("athletes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("role", sa.String(20), nullable=False, server_default="member"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_shared_chat_memberships_active", "shared_chat_memberships", ["active"]
    )
    with op.batch_alter_table("invitations") as batch:
        batch.add_column(sa.Column("target_telegram_user_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("shared_chat_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"))
        batch.create_foreign_key(
            "fk_invitations_shared_chat",
            "shared_chats",
            ["shared_chat_id"],
            ["telegram_chat_id"],
        )

    # Preserve every legacy sharing opt-in. A user who had /sharing off has a
    # NULL preference and is deliberately not re-enabled.
    op.execute(
        sa.text(
            "INSERT INTO shared_chats "
            "(telegram_chat_id, title, created_by, active, created_at) "
            "SELECT shared_chat_id, NULL, MIN(athlete_id), true, CURRENT_TIMESTAMP "
            "FROM training_preferences WHERE shared_chat_id IS NOT NULL "
            "GROUP BY shared_chat_id"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO shared_chat_memberships "
            "(telegram_chat_id, athlete_id, role, active, created_at) "
            "SELECT shared_chat_id, athlete_id, 'member', true, CURRENT_TIMESTAMP "
            "FROM training_preferences WHERE shared_chat_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("invitations") as batch:
        batch.drop_constraint("fk_invitations_shared_chat", type_="foreignkey")
        batch.drop_column("payload")
        batch.drop_column("shared_chat_id")
        batch.drop_column("target_telegram_user_id")
    op.drop_index(
        "ix_shared_chat_memberships_active", table_name="shared_chat_memberships"
    )
    op.drop_table("shared_chat_memberships")
    op.drop_index("ix_shared_chats_created_by", table_name="shared_chats")
    op.drop_table("shared_chats")
