"""Store encrypted operator configuration managed through the GUI."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_gui_configuration"
down_revision: str | None = "0002_sync_pairs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_configuration",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("settings_ciphertext", sa.Text(), nullable=False),
        sa.Column("credential_key_id", sa.String(length=128), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("app_configuration")
