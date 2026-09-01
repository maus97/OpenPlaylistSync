"""Preserve verified mappings while preparing ytmusicapi search caching.

Revision ID: 0011_ytmusicapi_migration
Revises: 0010_review_track_candidates
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_ytmusicapi_migration"
down_revision: str | None = "0010_review_track_candidates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("provider_track_mappings") as batch:
        batch.add_column(
            sa.Column("source_provider_track_id", sa.String(length=255), nullable=True)
        )
        batch.add_column(sa.Column("source_isrc", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_provider_track_mappings_source_track",
        "provider_track_mappings",
        ["pair_id", "account_id", "source_provider_track_id"],
    )
    op.create_index(
        "ix_provider_track_mappings_source_isrc",
        "provider_track_mappings",
        ["pair_id", "account_id", "source_isrc"],
    )
    op.create_table(
        "provider_search_cache",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("track_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("algorithm_version", sa.Integer(), nullable=False),
        sa.Column("resolved_track_json", sa.Text(), nullable=True),
        sa.Column("candidate_tracks_json", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["account_id"], ["provider_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "provider_name",
            "track_fingerprint",
            "algorithm_version",
            name="uq_provider_search_cache_identity",
        ),
    )
    # Successful past writes are verified mapping evidence, independent of the
    # provider-display metadata change that invalidates the sync baseline.
    op.execute("UPDATE provider_track_mappings SET identity_version = 3")


def downgrade() -> None:
    op.drop_table("provider_search_cache")
    op.drop_index("ix_provider_track_mappings_source_isrc", table_name="provider_track_mappings")
    op.drop_index("ix_provider_track_mappings_source_track", table_name="provider_track_mappings")
    with op.batch_alter_table("provider_track_mappings") as batch:
        batch.drop_column("source_isrc")
        batch.drop_column("source_provider_track_id")
    op.execute("UPDATE provider_track_mappings SET identity_version = 2 WHERE identity_version = 3")
