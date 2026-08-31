"""Add initial-sync policy and durable action journal."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_sync_hardening"
down_revision: str | None = "0003_gui_configuration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sync_pairs") as batch:
        batch.add_column(
            sa.Column(
                "initial_sync_policy", sa.String(length=32), nullable=False, server_default="merge"
            )
        )
    with op.batch_alter_table("sync_runs") as batch:
        batch.add_column(sa.Column("pair_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("plan_fingerprint", sa.String(length=1024), nullable=True))
        batch.create_foreign_key("fk_sync_runs_pair_id", "sync_pairs", ["pair_id"], ["id"])
    op.create_table(
        "sync_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("track_key", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["sync_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("sync_actions")
    with op.batch_alter_table("sync_runs") as batch:
        batch.drop_constraint("fk_sync_runs_pair_id", type_="foreignkey")
        batch.drop_column("plan_fingerprint")
        batch.drop_column("pair_id")
    with op.batch_alter_table("sync_pairs") as batch:
        batch.drop_column("initial_sync_policy")
