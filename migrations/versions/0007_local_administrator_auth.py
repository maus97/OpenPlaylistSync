"""Add local administrator authentication and sign-in rate-limit state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_local_administrator_auth"
down_revision: str | None = "0006_provider_track_mappings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "local_administrator",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "login_rate_limits",
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("source_key"),
    )


def downgrade() -> None:
    op.drop_table("login_rate_limits")
    op.drop_table("local_administrator")
