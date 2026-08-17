"""Synchronization engine seam; reconciliation execution is future work."""

from typing import Protocol


class SynchronizationEngine(Protocol):
    """Contract for future three-way synchronization orchestration."""

    def plan(self, *, playlist_key: str, dry_run: bool = True) -> object:
        """Build a non-destructive plan from baseline and provider snapshots."""

        ...
