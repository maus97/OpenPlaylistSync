"""Invalidate existing browser sessions when the local password changes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_invalidate_sessions_on_password_change"
down_revision: str | None = "0007_local_administrator_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "local_administrator",
        sa.Column("session_generation", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("local_administrator", "session_generation")
