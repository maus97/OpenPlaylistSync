"""Persistence-aware synchronization planning and execution coordinator."""

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ops.config import Settings
from ops.models import ProviderAccount, SyncBaseline, SyncPair
from ops.security.crypto import CredentialCipher
from ops.storage.repositories import (
    ProviderAccountRepository,
    SyncBaselineRepository,
    SyncRunRepository,
)
from ops.sync.domain import BaselineState, PlaylistState, ReconciliationPlan, reconcile
from ops.sync.executor import SyncExecutor, SyncProvider
from ops.sync.safety import Approval
from ops.sync.serialization import decode_baseline, encode_baseline

ProviderFactory = Callable[[ProviderAccount, dict[str, Any]], SyncProvider]


class SyncCoordinator:
    """Load state, create plans, and persist only successful baselines."""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        provider_factory: ProviderFactory,
    ) -> None:
        self.session = session
        self.settings = settings
        self.provider_factory = provider_factory
        self.cipher = CredentialCipher(settings.credential_encryption_key or "")

    def _providers(
        self, pair: SyncPair
    ) -> tuple[SyncProvider, SyncProvider, ProviderAccount, ProviderAccount]:
        source_account = self.session.get(ProviderAccount, pair.source_account_id)
        target_account = self.session.get(ProviderAccount, pair.target_account_id)
        if source_account is None or target_account is None:
            raise ValueError("sync pair references a missing provider account")
        account_repo = ProviderAccountRepository(self.session, self.cipher)
        source_provider = self.provider_factory(
            source_account, account_repo.load_credentials(source_account)
        )
        target_provider = self.provider_factory(
            target_account, account_repo.load_credentials(target_account)
        )
        return source_provider, target_provider, source_account, target_account

    def preview(self, pair: SyncPair) -> ReconciliationPlan:
        source_provider, target_provider, _, _ = self._providers(pair)
        source = PlaylistState.from_provider_playlist(
            source_provider.get_playlist(pair.source_playlist_id)
        )
        target = PlaylistState.from_provider_playlist(
            target_provider.get_playlist(pair.target_playlist_id)
        )
        baseline_record = SyncBaselineRepository(self.session).latest_for_pair(pair.id)
        baseline: BaselineState | None = (
            decode_baseline(baseline_record.snapshot_json) if baseline_record else None
        )
        plan = reconcile(baseline, source, target)
        run_repo = SyncRunRepository(self.session)
        run = run_repo.start(baseline_record.id if baseline_record else None)
        run_repo.finish(
            run,
            "conflict" if plan.conflicts else "planned",
            json.dumps({"actions": len(plan.actions), "conflicts": len(plan.conflicts)}),
        )
        self.session.commit()
        return plan

    def apply(
        self,
        pair: SyncPair,
        plan: ReconciliationPlan,
        approval: Approval | None = None,
    ) -> None:
        source_provider, target_provider, _, _ = self._providers(pair)
        current_source = PlaylistState.from_provider_playlist(
            source_provider.get_playlist(pair.source_playlist_id)
        )
        current_target = PlaylistState.from_provider_playlist(
            target_provider.get_playlist(pair.target_playlist_id)
        )
        baseline_record = SyncBaselineRepository(self.session).latest_for_pair(pair.id)
        baseline = decode_baseline(baseline_record.snapshot_json) if baseline_record else None
        current_plan = reconcile(baseline, current_source, current_target)
        if current_plan != plan:
            raise ValueError("provider state changed; discard the old plan and preview again")
        SyncExecutor().apply(
            plan,
            source_provider=source_provider,
            target_provider=target_provider,
            source_playlist_id=pair.source_playlist_id,
            target_playlist_id=pair.target_playlist_id,
            approval=approval,
        )
        if not plan.actions:
            return
        resulting_source = PlaylistState.from_provider_playlist(
            source_provider.get_playlist(pair.source_playlist_id)
        )
        resulting_target = PlaylistState.from_provider_playlist(
            target_provider.get_playlist(pair.target_playlist_id)
        )
        baseline_repo = SyncBaselineRepository(self.session)
        baseline_repo.save(
            SyncBaseline(
                pair_id=pair.id,
                account_id=pair.source_account_id,
                playlist_key=f"{pair.source_playlist_id}:{pair.target_playlist_id}",
                source_provider=resulting_source.provider,
                target_provider=resulting_target.provider,
                snapshot_json=encode_baseline(
                    BaselineState(source=resulting_source, target=resulting_target)
                ),
                synchronized_at=datetime.now().astimezone(),
            )
        )
        self.session.commit()
