"""Add review approvals, pair leases, identity versions, and account identity."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_security_remediation_state"
down_revision: str | None = "0008_invalidate_sessions_on_password_change"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_pair_scoped_mappings() -> None:
    op.create_table(
        "provider_track_mappings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pair_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("provider_track_id", sa.String(length=255), nullable=False),
        sa.Column("canonical_key", sa.String(length=1024), nullable=False),
        sa.Column(
            "provenance",
            sa.String(length=32),
            nullable=False,
            server_default="successful_add",
        ),
        sa.Column("identity_version", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["provider_accounts.id"]),
        sa.ForeignKeyConstraint(["pair_id"], ["sync_pairs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pair_id", "account_id", "provider_track_id"),
    )


def _create_legacy_mappings() -> None:
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


def upgrade() -> None:
    with op.batch_alter_table("provider_accounts") as batch:
        batch.add_column(sa.Column("display_name", sa.String(length=255), nullable=True))
    with op.batch_alter_table("sync_pairs") as batch:
        batch.add_column(sa.Column("operation_lock_token", sa.String(length=128), nullable=True))
        batch.add_column(
            sa.Column("operation_lock_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
    with op.batch_alter_table("sync_baselines") as batch:
        batch.add_column(
            sa.Column("identity_version", sa.Integer(), nullable=False, server_default="1")
        )
    with op.batch_alter_table("sync_runs") as batch:
        batch.add_column(sa.Column("plan_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("resolution_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("source_state_hash", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("target_state_hash", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("approval_token_hash", sa.String(length=128), nullable=True))
        batch.add_column(
            sa.Column("approval_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("approval_consumed_at", sa.DateTime(timezone=True), nullable=True)
        )
    op.create_index(
        "ix_sync_runs_pair_status_started",
        "sync_runs",
        ["pair_id", "status", "started_at"],
    )

    # Mappings are a rebuildable cache. Legacy rows have no pair provenance and
    # cannot be migrated safely without risking cross-pair identity corruption.
    op.drop_table("provider_track_mappings")
    _create_pair_scoped_mappings()


def downgrade() -> None:
    # Pair-scoped mappings cannot be collapsed safely to the legacy global key.
    op.drop_table("provider_track_mappings")
    _create_legacy_mappings()
    op.drop_index("ix_sync_runs_pair_status_started", table_name="sync_runs")
    with op.batch_alter_table("sync_runs") as batch:
        batch.drop_column("approval_consumed_at")
        batch.drop_column("approval_expires_at")
        batch.drop_column("approval_token_hash")
        batch.drop_column("target_state_hash")
        batch.drop_column("source_state_hash")
        batch.drop_column("resolution_json")
        batch.drop_column("plan_json")
    with op.batch_alter_table("sync_baselines") as batch:
        batch.drop_column("identity_version")
    with op.batch_alter_table("sync_pairs") as batch:
        batch.drop_column("operation_lock_expires_at")
        batch.drop_column("operation_lock_token")
    with op.batch_alter_table("provider_accounts") as batch:
        batch.drop_column("display_name")
