"""Persist verified cross-provider track identities."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_provider_track_mappings"
down_revision: str | None = "0005_remove_demo_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_track_mappings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("provider_track_id", sa.String(length=255), nullable=False),
        sa.Column("canonical_key", sa.String(length=1024), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["provider_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "provider_track_id"),
    )


def downgrade() -> None:
    op.drop_table("provider_track_mappings")
