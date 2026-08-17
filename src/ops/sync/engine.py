"""Public synchronization engine contract."""

from typing import Protocol


class SynchronizationEngine(Protocol):
    """Contract for higher-level three-way synchronization orchestration."""

    def plan(self, *, playlist_key: str, dry_run: bool = True) -> object:
        """Build a non-destructive plan from baseline and provider snapshots."""

        ...
