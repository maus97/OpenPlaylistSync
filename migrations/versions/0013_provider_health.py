"""Persist connection verification and scoped retry incidents without altering history."""

import sqlalchemy as sa
from alembic import op

revision = "0013_provider_health"
down_revision = "0012_sync_mode"
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auth_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_lock_token", sa.String(128), nullable=True),
        sa.Column("refresh_lock_until", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("provider_accounts", column)
    op.create_table(
        "provider_incidents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "account_id", sa.Integer(), sa.ForeignKey("provider_accounts.id"), nullable=False
        ),
        sa.Column("pair_id", sa.Integer(), sa.ForeignKey("sync_pairs.id")),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("resource", sa.String(255)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_provider_incidents_account_id", "provider_incidents", ["account_id"])
    op.create_index("ix_provider_incidents_pair_id", "provider_incidents", ["pair_id"])


def downgrade():
    op.drop_table("provider_incidents")
    with op.batch_alter_table("provider_accounts") as batch:
        for name in ("refresh_lock_until", "refresh_lock_token", "auth_verified_at", "verified_at"):
            batch.drop_column(name)
