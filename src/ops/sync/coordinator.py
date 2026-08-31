"""Persistence-aware planning, credential refresh, and recoverable execution."""

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ops.auth.spotify import SpotifyOAuthConfig, SpotifyOAuthService
from ops.auth.youtube_music import YouTubeMusicAuthService
from ops.config import Settings
from ops.models import ProviderAccount, ProviderTrackMapping, SyncBaseline, SyncPair
from ops.providers.types import ProviderTrack
from ops.security.crypto import CredentialCipher
from ops.storage.repositories import (
    ProviderAccountRepository,
    SyncActionRepository,
    SyncBaselineRepository,
    SyncRunRepository,
)
from ops.sync.domain import (
    BaselineState,
    InitialSyncPolicy,
    PlaylistState,
    ReconciliationPlan,
    reconcile,
)
from ops.sync.executor import SyncExecutor, SyncProvider
from ops.sync.safety import Approval, plan_fingerprint
from ops.sync.serialization import decode_baseline, encode_baseline

ProviderFactory = Callable[[ProviderAccount, dict[str, Any]], SyncProvider]


class SyncCoordinator:
    """Load state, make safe plans, and only advance verified baselines."""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        provider_factory: ProviderFactory,
    ) -> None:
        self.session = session
        self.settings = settings
        self.provider_factory = provider_factory
        self.cipher = (
            CredentialCipher(settings.credential_encryption_key)
            if settings.credential_encryption_key
            else None
        )

    def _refresh_spotify_credentials(
        self, account: ProviderAccount, credentials: dict[str, Any]
    ) -> dict[str, Any]:
        """Refresh an expiring Spotify access token and persist rotated values."""

        expires_at = credentials.get("expires_at")
        if not expires_at:
            return credentials
        try:
            expiry = datetime.fromisoformat(str(expires_at)).astimezone(UTC)
        except ValueError:
            return credentials
        if expiry > datetime.now(UTC):
            return credentials
        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            raise ValueError("Spotify authorization expired; reconnect the account")
        if not self.settings.spotify_client_id or not self.settings.spotify_client_secret:
            raise ValueError("Spotify OAuth settings are incomplete")
        refreshed = SpotifyOAuthService(
            SpotifyOAuthConfig(
                self.settings.spotify_client_id,
                self.settings.spotify_client_secret,
                self.settings.spotify_redirect_uri,
            )
        ).refresh_token(str(refresh_token))
        merged = {
            **credentials,
            **refreshed,
            "refresh_token": refreshed.get("refresh_token", refresh_token),
            "expires_at": (datetime.now(UTC).timestamp() + int(refreshed.get("expires_in", 3600))),
        }
        merged["expires_at"] = datetime.fromtimestamp(float(merged["expires_at"]), UTC).isoformat()
        if self.cipher is None:
            raise ValueError("credential encryption is not configured")
        ProviderAccountRepository(self.session, self.cipher).save_credentials(account, merged)
        self.session.commit()
        return merged

    def _refresh_youtube_music_credentials(
        self, account: ProviderAccount, credentials: dict[str, Any]
    ) -> dict[str, Any]:
        """Refresh a Google OAuth access token before official API calls."""

        expires_at = credentials.get("expires_at")
        try:
            expired = not expires_at or datetime.fromisoformat(str(expires_at)).astimezone(
                UTC
            ) <= datetime.now(UTC)
        except ValueError:
            expired = True
        if not expired:
            return credentials
        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            raise ValueError("YouTube Music authorization expired; reconnect the account")
        if not self.settings.ytmusic_client_id or not self.settings.ytmusic_client_secret:
            raise ValueError("YouTube Music OAuth settings are incomplete")
        refreshed = YouTubeMusicAuthService(
            self.settings.ytmusic_client_id, self.settings.ytmusic_client_secret
        ).refresh_token(str(refresh_token))
        merged = {
            **credentials,
            **refreshed,
            "refresh_token": refreshed.get("refresh_token", refresh_token),
            "expires_at": (
                datetime.now(UTC) + timedelta(seconds=int(refreshed.get("expires_in", 3600)))
            ).isoformat(),
        }
        if self.cipher is None:
            raise ValueError("credential encryption is not configured")
        ProviderAccountRepository(self.session, self.cipher).save_credentials(account, merged)
        self.session.commit()
        return merged

    def _credentials(self, account: ProviderAccount) -> dict[str, Any]:
        if self.cipher is None:
            raise ValueError("credential encryption is not configured")
        credentials = ProviderAccountRepository(self.session, self.cipher).load_credentials(account)
        if account.provider_name == "spotify":
            credentials = self._refresh_spotify_credentials(account, credentials)
        if account.provider_name == "youtube_music":
            credentials = self._refresh_youtube_music_credentials(account, credentials)
        return credentials

    def _providers(
        self, pair: SyncPair
    ) -> tuple[SyncProvider, SyncProvider, ProviderAccount, ProviderAccount]:
        source_account = self.session.get(ProviderAccount, pair.source_account_id)
        target_account = self.session.get(ProviderAccount, pair.target_account_id)
        if source_account is None or target_account is None:
            raise ValueError("sync pair references a missing provider account")
        source_provider = self.provider_factory(source_account, self._credentials(source_account))
        target_provider = self.provider_factory(target_account, self._credentials(target_account))
        return source_provider, target_provider, source_account, target_account

    @staticmethod
    def _policy(pair: SyncPair) -> InitialSyncPolicy:
        try:
            return InitialSyncPolicy(pair.initial_sync_policy)
        except ValueError:
            return InitialSyncPolicy.MERGE

    def _current_state(
        self, pair: SyncPair
    ) -> tuple[PlaylistState, PlaylistState, SyncProvider, SyncProvider]:
        source_provider, target_provider, source_account, target_account = self._providers(pair)
        source = self._apply_track_mappings(
            source_account.id,
            PlaylistState.from_provider_playlist(
                source_provider.get_playlist(pair.source_playlist_id)
            ),
        )
        target = self._apply_track_mappings(
            target_account.id,
            PlaylistState.from_provider_playlist(
                target_provider.get_playlist(pair.target_playlist_id)
            ),
        )
        return source, target, source_provider, target_provider

    def _apply_track_mappings(self, account_id: int, state: PlaylistState) -> PlaylistState:
        """Use previously verified cross-provider matches when reading snapshots."""

        track_ids = {track.source_provider_track_id for track in state.tracks}
        if not track_ids:
            return state
        mappings = {
            mapping.provider_track_id: mapping.canonical_key
            for mapping in self.session.scalars(
                select(ProviderTrackMapping).where(
                    ProviderTrackMapping.account_id == account_id,
                    ProviderTrackMapping.provider_track_id.in_(track_ids),
                )
            )
        }
        if not mappings:
            return state
        return replace(
            state,
            tracks=tuple(
                replace(track, key=mappings.get(track.source_provider_track_id, track.key))
                for track in state.tracks
            ),
        )

    def _save_track_mapping(
        self, account_id: int, track: ProviderTrack, canonical_key: str
    ) -> None:
        """Remember the exact destination ID returned by a successful add."""

        mapping = self.session.scalar(
            select(ProviderTrackMapping).where(
                ProviderTrackMapping.account_id == account_id,
                ProviderTrackMapping.provider_track_id == track.provider_track_id,
            )
        )
        if mapping is None:
            mapping = ProviderTrackMapping(
                account_id=account_id,
                provider_track_id=track.provider_track_id,
                canonical_key=canonical_key,
            )
        else:
            mapping.canonical_key = canonical_key
        self.session.add(mapping)

    def _save_baseline(
        self, pair: SyncPair, source: PlaylistState, target: PlaylistState
    ) -> SyncBaseline:
        baseline = SyncBaseline(
            pair_id=pair.id,
            account_id=pair.source_account_id,
            playlist_key=f"{pair.source_playlist_id}:{pair.target_playlist_id}",
            source_provider=source.provider,
            target_provider=target.provider,
            snapshot_json=encode_baseline(BaselineState(source=source, target=target)),
            synchronized_at=datetime.now().astimezone(),
        )
        return SyncBaselineRepository(self.session).save(baseline)

    def preview(self, pair: SyncPair) -> ReconciliationPlan:
        source, target, _, _ = self._current_state(pair)
        baseline_record = SyncBaselineRepository(self.session).latest_for_pair(pair.id)
        baseline = decode_baseline(baseline_record.snapshot_json) if baseline_record else None
        plan = reconcile(baseline, source, target, initial_policy=self._policy(pair))
        run_repo = SyncRunRepository(self.session)
        run = run_repo.start(
            baseline_record.id if baseline_record else None,
            pair_id=pair.id,
            fingerprint=plan_fingerprint(plan),
        )
        run_repo.finish(
            run,
            "conflict" if plan.conflicts else "planned",
            json.dumps(
                {
                    "actions": len(plan.actions),
                    "conflicts": len(plan.conflicts),
                    "initial_sync": plan.initial_sync,
                    "policy": plan.initial_policy.value if plan.initial_policy else None,
                }
            ),
        )
        self.session.commit()
        return plan

    def accept_current_state(self, pair: SyncPair) -> None:
        """Explicitly create a baseline without attempting convergence."""

        source, target, _, _ = self._current_state(pair)
        baseline = self._save_baseline(pair, source, target)
        run = SyncRunRepository(self.session).start(baseline.id, pair_id=pair.id)
        SyncRunRepository(self.session).finish(run, "baseline_accepted")
        self.session.commit()

    def apply(
        self,
        pair: SyncPair,
        plan: ReconciliationPlan,
        approval: Approval | None = None,
    ) -> None:
        current_source, current_target, source_provider, target_provider = self._current_state(pair)
        baseline_record = SyncBaselineRepository(self.session).latest_for_pair(pair.id)
        baseline = decode_baseline(baseline_record.snapshot_json) if baseline_record else None
        current_plan = reconcile(
            baseline, current_source, current_target, initial_policy=self._policy(pair)
        )
        if current_plan != plan:
            raise ValueError("provider state changed; discard the old plan and preview again")

        run_repo = SyncRunRepository(self.session)
        run = run_repo.start(
            baseline_record.id if baseline_record else None,
            pair_id=pair.id,
            fingerprint=plan_fingerprint(plan),
        )
        action_repo = SyncActionRepository(self.session)
        journal = [
            action_repo.plan(
                run,
                ordinal,
                "source" if action.side.value == "source" else "target",
                action.action.value,
                action.track.key,
            )
            for ordinal, action in enumerate(plan.actions)
        ]
        self.session.commit()

        try:
            SyncExecutor().apply(
                plan,
                source_provider=source_provider,
                target_provider=target_provider,
                source_playlist_id=pair.source_playlist_id,
                target_playlist_id=pair.target_playlist_id,
                approval=approval,
                on_action_completed=lambda index: action_repo.complete(journal[index]),
                on_track_resolved=lambda action, track: self._save_track_mapping(
                    pair.source_account_id
                    if action.side.value == "source"
                    else pair.target_account_id,
                    track,
                    action.track.key,
                ),
            )
            resulting_source, resulting_target, _, _ = self._current_state(pair)
            self._save_baseline(pair, resulting_source, resulting_target)
            run_repo.finish(run, "applied", json.dumps({"actions": len(plan.actions)}))
            self.session.commit()
        except Exception as exc:
            for action in journal:
                if action.status == "planned":
                    action_repo.fail(action, str(exc))
                    break
            run_repo.finish(run, "failed", json.dumps({"error": str(exc)[:500]}))
            self.session.commit()
            raise
