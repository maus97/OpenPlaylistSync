"""Remove obsolete synthetic demo accounts and their derived records."""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_remove_demo_provider"
down_revision: str | None = "0004_sync_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Delete only data belonging to the retired demo providers."""

    demo_accounts = "(SELECT id FROM provider_accounts WHERE provider_name LIKE 'demo_%')"
    demo_pairs = (
        "(SELECT id FROM sync_pairs WHERE source_account_id IN "
        f"{demo_accounts} OR target_account_id IN {demo_accounts})"
    )
    demo_baselines = (
        "(SELECT id FROM sync_baselines WHERE pair_id IN "
        f"{demo_pairs} OR account_id IN {demo_accounts})"
    )
    demo_runs = (
        "(SELECT id FROM sync_runs WHERE pair_id IN "
        f"{demo_pairs} OR baseline_id IN {demo_baselines})"
    )
    op.execute(f"DELETE FROM sync_actions WHERE run_id IN {demo_runs}")
    op.execute(f"DELETE FROM sync_runs WHERE id IN {demo_runs}")
    op.execute(f"DELETE FROM sync_baselines WHERE id IN {demo_baselines}")
    op.execute(f"DELETE FROM sync_pairs WHERE id IN {demo_pairs}")
    op.execute(f"DELETE FROM provider_accounts WHERE id IN {demo_accounts}")


def downgrade() -> None:
    """Deleted synthetic data cannot and should not be restored."""
