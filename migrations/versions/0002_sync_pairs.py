"""Add configured bidirectional playlist pairs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_sync_pairs"
down_revision: str | None = "0001_initial_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sync_pairs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_account_id", sa.Integer(), nullable=False),
        sa.Column("target_account_id", sa.Integer(), nullable=False),
        sa.Column("source_playlist_id", sa.String(length=255), nullable=False),
        sa.Column("target_playlist_id", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["source_account_id"], ["provider_accounts.id"]),
        sa.ForeignKeyConstraint(["target_account_id"], ["provider_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_account_id",
            "target_account_id",
            "source_playlist_id",
            "target_playlist_id",
        ),
    )
    with op.batch_alter_table("sync_baselines", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("pair_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_sync_baselines_pair_id", "sync_pairs", ["pair_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("sync_baselines", recreate="always") as batch_op:
        batch_op.drop_constraint("fk_sync_baselines_pair_id", type_="foreignkey")
        batch_op.drop_column("pair_id")
    op.drop_table("sync_pairs")
