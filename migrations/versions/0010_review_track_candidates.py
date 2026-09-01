"""Persist operator-selectable close matches for a review."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_review_track_candidates"
down_revision: str | None = "0009_security_remediation_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sync_runs") as batch:
        batch.add_column(sa.Column("candidate_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sync_runs") as batch:
        batch.drop_column("candidate_json")
