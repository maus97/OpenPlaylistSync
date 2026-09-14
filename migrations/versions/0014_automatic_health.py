"""Durable automatic-sync diagnostics and cross-worker job exclusion."""

import sqlalchemy as sa
from alembic import op

revision = "0014_automatic_health"
down_revision = "0013_provider_health"
branch_labels = None
depends_on = None

DATES = (
    "automatic_lock_expires_at",
    "automatic_checked_at",
    "automatic_attempted_at",
    "automatic_succeeded_at",
    "automatic_next_at",
)


def upgrade():
    for name in DATES:
        op.add_column("sync_pairs", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    op.add_column("sync_pairs", sa.Column("automatic_lock_token", sa.String(128), nullable=True))
    op.add_column("sync_pairs", sa.Column("automatic_outcome", sa.String(512), nullable=True))


def downgrade():
    with op.batch_alter_table("sync_pairs") as batch:
        for name in (*DATES, "automatic_lock_token", "automatic_outcome"):
            batch.drop_column(name)
