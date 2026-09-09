"""Add one-way source-controlled sync pairs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_sync_mode"
down_revision: str | None = "0011_ytmusicapi_migration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sync_pairs") as batch:
        batch.add_column(
            sa.Column("sync_mode", sa.String(length=32), nullable=False, server_default="two_way")
        )


def downgrade() -> None:
    with op.batch_alter_table("sync_pairs") as batch:
        batch.drop_column("sync_mode")
