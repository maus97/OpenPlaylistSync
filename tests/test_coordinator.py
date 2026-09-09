from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ops.config import Settings
from ops.db import Base
from ops.models import (
    ProviderAccount,
    ProviderSearchCache,
    ProviderTrackMapping,
    SyncAction,
    SyncPair,
    SyncRun,
)
from ops.providers.base import AuthorizationRequired, ProviderUnavailable, TrackUnavailable
from ops.providers.types import ProviderPlaylist, ProviderTrack
from ops.storage.repositories import SyncActionRepository, SyncBaselineRepository, SyncRunRepository
from ops.sync.automatic import automation_binding
from ops.sync.coordinator import (
    AmbiguousSpotifyRemoval,
    PartialSyncRecoveryRequired,
    SyncCoordinator,
    TrackMappingConflict,
    _encode_resolutions,
)
from ops.sync.domain import (
    ActionType,
    InitialSyncPolicy,
    PlaylistState,
    ReconciliationAction,
    ReconciliationPlan,
    Side,
    TrackState,
)
from ops.sync.leases import PairOperationBusy, acquire_pair_lease
from ops.sync.safety import Approval, plan_fingerprint
from ops.sync.serialization import encode_plan


class InMemoryProvider:
    """Small provider fake used to exercise the real coordinator seam."""

    def __init__(self, provider: str, playlist_id: str, tracks: Sequence[ProviderTrack]) -> None:
        self.provider = provider
        self.name = provider
        self.playlist_id = playlist_id
        self.tracks = list(tracks)
        self.search_calls = 0
        self.snapshot = "snapshot-0"

    def get_playlist(self, playlist_id: str) -> ProviderPlaylist:
        assert playlist_id == self.playlist_id
        return ProviderPlaylist(
            playlist_id,
            "Test playlist",
            tuple(self.tracks),
            snapshot_id=self.snapshot,
        )

    def search_track(self, track: ProviderTrack) -> ProviderTrack:
        self.search_calls += 1
        title = (
            f"{track.artists[0]} - {track.title} (Official Video)"
            if self.provider == "youtube_music"
            else track.title
        )
        return ProviderTrack(f"{self.provider}:resolved-{len(self.tracks)}", title, track.artists)

    def add_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> str:
        assert playlist_id == self.playlist_id
        self.tracks.extend(tracks)
        self.snapshot = f"snapshot-{len(self.tracks)}"
        return self.snapshot

    def remove_tracks(
        self,
        playlist_id: str,
        tracks: Sequence[ProviderTrack],
        *,
        snapshot_id: str | None = None,
    ) -> str:
        del snapshot_id
        assert playlist_id == self.playlist_id
        for track in tracks:
            match = next(
                item for item in self.tracks if item.provider_track_id == track.provider_track_id
            )
            self.tracks.remove(match)
        self.snapshot = f"snapshot-{len(self.tracks)}"
        return self.snapshot


def test_initial_merge_applies_additions_journals_actions_and_creates_baseline() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="test_source", external_account_id="source")
        target = ProviderAccount(provider_name="test_target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="spotify:source-playlist",
            target_playlist_id="youtube_music:target-playlist",
            initial_sync_policy=InitialSyncPolicy.MERGE.value,
        )
        session.add(pair)
        session.commit()

        providers = {
            source.id: InMemoryProvider(
                "spotify",
                "spotify:source-playlist",
                [ProviderTrack("spotify:track-1", "Song", ("Artist",))],
            ),
            target.id: InMemoryProvider("youtube_music", "youtube_music:target-playlist", []),
        }
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )
        review = coordinator.prepare_review(pair)
        plan = review.plan
        assert plan.initial_sync
        assert len(plan.actions) == 1
        assert review.source_track_count == 1
        assert review.target_track_count == 0

        assert coordinator.unresolved_actions(pair, plan) == ()
        assert providers[target.id].search_calls == 1

        coordinator.apply(
            pair,
            plan,
            Approval(
                plan_fingerprint(plan),
                "",
                review_id=review.review_id,
                token=review.approval_token,
            ),
        )
        assert providers[target.id].search_calls == 1

        baseline = SyncBaselineRepository(session).latest_for_pair(pair.id)
        assert baseline is not None
        run = SyncRunRepository(session).recent(1)[0]
        assert run.status == "applied"
        assert [action.status for action in SyncActionRepository(session).for_run(run.id)] == [
            "completed"
        ]
        providers[source.id].tracks.clear()
        removal_plan = coordinator.preview(pair)
        assert len(removal_plan.actions) == 1
        assert removal_plan.actions[0].action.value == "remove_track"
    engine.dispose()


def test_review_looks_up_duplicate_tracks_once() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="test_source", external_account_id="source")
        target = ProviderAccount(provider_name="test_target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="spotify:source-playlist",
            target_playlist_id="youtube_music:target-playlist",
            initial_sync_policy=InitialSyncPolicy.MERGE.value,
        )
        session.add(pair)
        session.commit()

        duplicate_track = ProviderTrack("spotify:track-1", "Song", ("Artist",))
        providers = {
            source.id: InMemoryProvider("spotify", "spotify:source-playlist", [duplicate_track]),
            target.id: InMemoryProvider("youtube_music", "youtube_music:target-playlist", []),
        }
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )
        providers[source.id].tracks.append(duplicate_track)
        review = coordinator.prepare_review(pair)

        assert len(review.plan.actions) == 2
        assert review.unresolved_actions == ()
        assert providers[target.id].search_calls == 1
    engine.dispose()


def test_first_review_links_reciprocal_existing_tracks_without_adding_duplicates() -> None:
    class ExistingTrackProvider(InMemoryProvider):
        def __init__(
            self, provider: str, playlist_id: str, tracks: Sequence[ProviderTrack]
        ) -> None:
            super().__init__(provider, playlist_id, tracks)
            self.add_calls = 0

        def search_track(self, track: ProviderTrack) -> ProviderTrack | None:
            self.search_calls += 1
            return self.tracks[0]

        def add_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> str:
            self.add_calls += 1
            return super().add_tracks(playlist_id, tracks)

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="source", external_account_id="source")
        target = ProviderAccount(provider_name="target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="source:playlist",
            target_playlist_id="target:playlist",
        )
        session.add(pair)
        session.commit()
        source_provider = ExistingTrackProvider(
            "source",
            "source:playlist",
            [ProviderTrack("source:one", "Song", ("Artist",), duration_ms=180_000)],
        )
        target_provider = ExistingTrackProvider(
            "target",
            "target:playlist",
            [
                ProviderTrack(
                    "target:one",
                    "Artist - Song (Official Audio)",
                    ("Artist - Topic",),
                    duration_ms=180_000,
                )
            ],
        )
        providers = {source.id: source_provider, target.id: target_provider}
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )

        review = coordinator.prepare_review(pair)

        assert review.plan.actions == ()
        assert review.unresolved_actions == ()
        mappings = list(session.scalars(select(ProviderTrackMapping)))
        assert len(mappings) == 2
        assert len({mapping.canonical_key for mapping in mappings}) == 1
        assert {mapping.provenance for mapping in mappings} == {"verified_equivalence"}
        coordinator.accept_current_state(pair)
        assert source_provider.add_calls == 0
        assert target_provider.add_calls == 0
        assert SyncBaselineRepository(session).latest_for_pair(pair.id) is not None
    engine.dispose()


def test_first_review_repairs_a_historical_alias_before_planning_writes() -> None:
    class ExistingTrackProvider(InMemoryProvider):
        def search_track(self, track: ProviderTrack) -> ProviderTrack:
            self.search_calls += 1
            return self.tracks[0]

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="source", external_account_id="source")
        target = ProviderAccount(provider_name="target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="source:playlist",
            target_playlist_id="target:playlist",
        )
        session.add(pair)
        session.flush()
        session.add(
            ProviderTrackMapping(
                pair_id=pair.id,
                account_id=target.id,
                provider_track_id="target:one",
                canonical_key="text:artist song official audio|artist topic",
                provenance="reciprocal_add",
            )
        )
        session.commit()

        source_provider = ExistingTrackProvider(
            "source",
            "source:playlist",
            [ProviderTrack("source:one", "Song", ("Artist",), duration_ms=180_000)],
        )
        target_provider = ExistingTrackProvider(
            "target",
            "target:playlist",
            [
                ProviderTrack(
                    "target:one",
                    "Artist - Song (Official Audio)",
                    ("Artist - Topic",),
                    duration_ms=180_000,
                )
            ],
        )
        providers = {source.id: source_provider, target.id: target_provider}
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )

        review = coordinator.prepare_review(pair)

        assert review.plan.actions == ()
        mappings = list(session.scalars(select(ProviderTrackMapping)))
        assert len(mappings) == 2
        assert len({mapping.canonical_key for mapping in mappings}) == 1
        assert source_provider.tracks == [source_provider.tracks[0]]
        assert target_provider.tracks == [target_provider.tracks[0]]
    engine.dispose()


def test_existing_recording_identity_does_not_hide_a_real_duplicate_count_gap() -> None:
    class ExistingTrackProvider(InMemoryProvider):
        def search_track(self, track: ProviderTrack) -> ProviderTrack:
            self.search_calls += 1
            return self.tracks[0]

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="source", external_account_id="source")
        target = ProviderAccount(provider_name="target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="source:playlist",
            target_playlist_id="target:playlist",
        )
        session.add(pair)
        session.commit()
        source_track = ProviderTrack("source:one", "Song", ("Artist",), duration_ms=180_000)
        source_provider = ExistingTrackProvider(
            "source", "source:playlist", [source_track, source_track]
        )
        target_provider = ExistingTrackProvider(
            "target",
            "target:playlist",
            [
                ProviderTrack(
                    "target:one",
                    "Artist - Song (Official Audio)",
                    ("Artist - Topic",),
                    duration_ms=180_000,
                )
            ],
        )
        providers = {source.id: source_provider, target.id: target_provider}
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )

        review = coordinator.prepare_review(pair)

        assert len(review.plan.actions) == 1
        assert review.plan.actions[0].action is ActionType.ADD_TRACK
        assert review.plan.actions[0].side is Side.TARGET
        assert review.unresolved_actions == ()
    engine.dispose()


def test_failed_first_sync_duplicate_is_not_propagated_by_a_recovery_review() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="source", external_account_id="source")
        target = ProviderAccount(provider_name="target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="source:playlist",
            target_playlist_id="target:playlist",
        )
        session.add(pair)
        session.flush()
        failed_plan = ReconciliationPlan(
            actions=(
                ReconciliationAction(
                    action=ActionType.ADD_TRACK,
                    side=Side.SOURCE,
                    track=TrackState(
                        key="text:artist song official audio|artist topic",
                        title="Artist - Song (Official Audio)",
                        artists=("Artist - Topic",),
                        source_provider_track_id="target:one",
                    ),
                    reason="missing from source",
                ),
            ),
            conflicts=(),
            initial_sync=True,
        )
        failed_run = SyncRun(
            pair_id=pair.id,
            status="failed",
            plan_json=encode_plan(failed_plan),
            resolution_json=_encode_resolutions(
                {0: ProviderTrack("source:one", "Song", ("Artist",))}
            ),
        )
        session.add(failed_run)
        session.flush()
        session.add(
            SyncAction(
                run_id=failed_run.id,
                ordinal=0,
                provider_name="source",
                operation=ActionType.ADD_TRACK.value,
                track_key=failed_plan.actions[0].track.key,
                status="completed",
            )
        )
        session.commit()

        duplicate = ProviderTrack("source:one", "Song", ("Artist",))
        providers = {
            source.id: InMemoryProvider("source", "source:playlist", [duplicate, duplicate]),
            target.id: InMemoryProvider(
                "target",
                "target:playlist",
                [
                    ProviderTrack(
                        "target:one",
                        "Artist - Song (Official Audio)",
                        ("Artist - Topic",),
                    )
                ],
            ),
        }
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )

        with pytest.raises(PartialSyncRecoveryRequired, match="extra copies"):
            coordinator.prepare_review(pair)

        assert len(providers[source.id].tracks) == 2
        assert len(providers[target.id].tracks) == 1
        assert SyncRunRepository(session).recent(1)[0].status == "review_failed"
    engine.dispose()


def test_review_rejects_internal_mapping_conflicts_before_any_playlist_write() -> None:
    class ConflictingDestinationProvider(InMemoryProvider):
        def search_track(self, track: ProviderTrack) -> ProviderTrack:
            self.search_calls += 1
            return ProviderTrack("target:shared", track.title, track.artists)

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="source", external_account_id="source")
        target = ProviderAccount(provider_name="target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="source:playlist",
            target_playlist_id="target:playlist",
        )
        session.add(pair)
        session.commit()
        source_provider = InMemoryProvider(
            "source",
            "source:playlist",
            [
                ProviderTrack("source:one", "One", ("Artist",)),
                ProviderTrack("source:two", "Two", ("Artist",)),
            ],
        )
        target_provider = ConflictingDestinationProvider("target", "target:playlist", [])
        providers = {source.id: source_provider, target.id: target_provider}
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )

        with pytest.raises(TrackMappingConflict, match="different songs"):
            coordinator.prepare_review(pair)

        assert target_provider.tracks == []
    engine.dispose()


def test_review_does_not_reuse_a_search_for_different_track_evidence() -> None:
    """Same title/artist can still be a different recording or release."""

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="test_source", external_account_id="source")
        target = ProviderAccount(provider_name="test_target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="spotify:source-playlist",
            target_playlist_id="youtube_music:target-playlist",
            initial_sync_policy=InitialSyncPolicy.MERGE.value,
        )
        session.add(pair)
        session.commit()

        providers = {
            source.id: InMemoryProvider(
                "spotify",
                "spotify:source-playlist",
                [
                    ProviderTrack(
                        "spotify:original",
                        "Song",
                        ("Artist",),
                        album="Original album",
                        duration_ms=180_000,
                        isrc="ISRC-ORIGINAL",
                    ),
                    ProviderTrack(
                        "spotify:remaster",
                        "Song",
                        ("Artist",),
                        album="Remaster album",
                        duration_ms=184_000,
                        isrc="ISRC-REMASTER",
                    ),
                ],
            ),
            target.id: InMemoryProvider("youtube_music", "youtube_music:target-playlist", []),
        }
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )

        review = coordinator.prepare_review(pair)

        assert len(review.plan.actions) == 2
        assert providers[target.id].search_calls == 2
    engine.dispose()


def test_operator_can_select_a_persisted_close_match_before_apply() -> None:
    class ManualCandidateProvider(InMemoryProvider):
        def search_track(self, track: ProviderTrack) -> ProviderTrack | None:
            self.search_calls += 1
            return None

        def close_track_candidates(self, track: ProviderTrack) -> Sequence[ProviderTrack]:
            return (
                ProviderTrack(
                    "youtube_music:manual-choice",
                    f"{track.title} (Live)",
                    track.artists,
                    duration_ms=track.duration_ms,
                ),
            )

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="source", external_account_id="source")
        target = ProviderAccount(provider_name="target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="source:playlist",
            target_playlist_id="target:playlist",
        )
        session.add(pair)
        session.commit()
        providers = {
            source.id: InMemoryProvider(
                "source", "source:playlist", [ProviderTrack("source:one", "One", ("A",))]
            ),
            target.id: ManualCandidateProvider("target", "target:playlist", []),
        }
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )

        review = coordinator.prepare_review(pair)
        assert len(review.unresolved_actions) == 1
        assert (
            review.candidate_options[0].candidates[0].provider_track_id
            == "youtube_music:manual-choice"
        )
        with pytest.raises(ValueError, match="not part of this review"):
            coordinator.select_candidate(pair, review.review_id, 0, "youtube_music:forged")

        selected = coordinator.select_candidate(
            pair, review.review_id, 0, "youtube_music:manual-choice"
        )
        assert selected.unresolved_actions == ()
        assert selected.candidate_options == ()
        coordinator.apply(pair, selected.plan, _approval(review))
        assert providers[target.id].tracks[0].provider_track_id == "youtube_music:manual-choice"
    engine.dispose()


def test_review_enriches_only_an_ambiguous_track_and_rescores_existing_candidates() -> None:
    class EnrichingSourceProvider(InMemoryProvider):
        def __init__(
            self, provider: str, playlist_id: str, tracks: Sequence[ProviderTrack]
        ) -> None:
            super().__init__(provider, playlist_id, tracks)
            self.enrich_calls = 0

        def enrich_track_metadata(self, track: ProviderTrack) -> ProviderTrack:
            self.enrich_calls += 1
            return ProviderTrack(
                track.provider_track_id,
                track.title,
                track.artists,
                album="Single release",
                duration_ms=track.duration_ms,
            )

    class AmbiguousDestinationProvider(InMemoryProvider):
        def __init__(
            self, provider: str, playlist_id: str, tracks: Sequence[ProviderTrack]
        ) -> None:
            super().__init__(provider, playlist_id, tracks)
            self.candidates = (
                ProviderTrack("target:album", "Song", ("Artist",), album="Album release"),
                ProviderTrack("target:single", "Song", ("Artist",), album="Single release"),
            )
            self.rescore_calls = 0

        def search_track(self, track: ProviderTrack) -> ProviderTrack | None:
            self.search_calls += 1
            return None

        def close_track_candidates(self, track: ProviderTrack) -> Sequence[ProviderTrack]:
            return self.candidates

        def resolve_search_candidates(
            self, track: ProviderTrack, candidates: Sequence[ProviderTrack]
        ) -> ProviderTrack | None:
            self.rescore_calls += 1
            return next(candidate for candidate in candidates if candidate.album == track.album)

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="source", external_account_id="source")
        target = ProviderAccount(provider_name="target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="source:playlist",
            target_playlist_id="target:playlist",
        )
        session.add(pair)
        session.commit()
        source_provider = EnrichingSourceProvider(
            "source",
            "source:playlist",
            [ProviderTrack("source:one", "Song", ("Artist",), duration_ms=180_000)],
        )
        target_provider = AmbiguousDestinationProvider("target", "target:playlist", [])
        providers = {source.id: source_provider, target.id: target_provider}
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )

        review = coordinator.prepare_review(pair)

        assert review.unresolved_actions == ()
        assert source_provider.enrich_calls == 1
        assert target_provider.search_calls == 1
        assert target_provider.rescore_calls == 1
        run = SyncRunRepository(session).get(review.review_id)
        assert run is not None
        assert '"provider_track_id":"target:single"' in (run.resolution_json or "")
    engine.dispose()


def test_manual_candidate_mapping_conflict_fails_before_a_playlist_write() -> None:
    class ManualCandidateProvider(InMemoryProvider):
        def search_track(self, track: ProviderTrack) -> ProviderTrack | None:
            self.search_calls += 1
            return None

        def close_track_candidates(self, track: ProviderTrack) -> Sequence[ProviderTrack]:
            return (ProviderTrack("youtube_music:shared", track.title, track.artists),)

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="source", external_account_id="source")
        target = ProviderAccount(provider_name="target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="source:playlist",
            target_playlist_id="target:playlist",
        )
        session.add(pair)
        session.flush()
        session.add(
            ProviderTrackMapping(
                pair_id=pair.id,
                account_id=target.id,
                provider_track_id="youtube_music:shared",
                canonical_key="text:another-song|artist",
                provenance="successful_add",
            )
        )
        session.commit()
        providers = {
            source.id: InMemoryProvider(
                "source", "source:playlist", [ProviderTrack("source:one", "One", ("Artist",))]
            ),
            target.id: ManualCandidateProvider("target", "target:playlist", []),
        }
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )

        review = coordinator.prepare_review(pair)
        selected = coordinator.select_candidate(pair, review.review_id, 0, "youtube_music:shared")

        with pytest.raises(TrackMappingConflict, match="different song"):
            coordinator.apply(pair, selected.plan, _approval(review))
        assert providers[target.id].tracks == []
    engine.dispose()


def _approval(review) -> Approval:  # type: ignore[no-untyped-def]
    return Approval(
        plan_fingerprint(review.plan),
        "APPLY DESTRUCTIVE CHANGES" if review.plan.requires_approval else "",
        review_id=review.review_id,
        token=review.approval_token,
    )


def test_apply_rejects_provider_drift_and_one_time_review_replay() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="test_source", external_account_id="source")
        target = ProviderAccount(provider_name="test_target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="spotify:source",
            target_playlist_id="youtube_music:target",
        )
        session.add(pair)
        session.commit()
        providers = {
            source.id: InMemoryProvider(
                "spotify", "spotify:source", [ProviderTrack("spotify:one", "One", ("A",))]
            ),
            target.id: InMemoryProvider("youtube_music", "youtube_music:target", []),
        }
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )
        stale_review = coordinator.prepare_review(pair)
        providers[source.id].tracks.append(ProviderTrack("spotify:two", "Two", ("B",)))
        providers[source.id].snapshot = "changed"
        with pytest.raises(ValueError, match="provider state changed"):
            coordinator.apply(pair, stale_review.plan, _approval(stale_review))
        assert providers[target.id].tracks == []

        providers[source.id].tracks.pop()
        providers[source.id].snapshot = "snapshot-0"
        with pytest.raises(ValueError, match="already used or cannot be applied"):
            coordinator.apply(pair, stale_review.plan, _approval(stale_review))
        assert session.get(SyncRun, stale_review.review_id).status == "stale"
        fresh_review = coordinator.prepare_review(pair)
        assert fresh_review.review_id != stale_review.review_id
        coordinator.apply(pair, fresh_review.plan, _approval(fresh_review))
        assert len(providers[target.id].tracks) == 1
        with pytest.raises((ValueError, PairOperationBusy)):
            coordinator.apply(pair, fresh_review.plan, _approval(fresh_review))
        assert len(providers[target.id].tracks) == 1
    engine.dispose()


@pytest.mark.parametrize("change", ["direction", "baseline", "legacy_review"])
def test_review_cannot_survive_configuration_or_baseline_change(change) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        accounts = [
            ProviderAccount(provider_name=n, external_account_id=n)
            for n in ("test_source", "test_target")
        ]
        session.add_all(accounts)
        session.flush()
        pair = SyncPair(
            source_account_id=accounts[0].id,
            target_account_id=accounts[1].id,
            source_playlist_id="source:p",
            target_playlist_id="target:p",
        )
        session.add(pair)
        session.commit()
        providers = {
            accounts[0].id: InMemoryProvider("source", "source:p", []),
            accounts[1].id: InMemoryProvider(
                "target", "target:p", [ProviderTrack("target:song", "Song", ("Artist",))]
            ),
        }
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode()),
            lambda account, _: providers[account.id],
        )
        review = coordinator.prepare_review(pair)
        assert review.plan.actions[0].side is Side.SOURCE
        if change == "direction":
            pair.sync_mode = "source_to_target"
            session.commit()
        elif change == "baseline":
            coordinator.accept_current_state(pair)
        else:
            session.get(SyncRun, review.review_id).summary_json = "{}"
            session.commit()
        with pytest.raises(ValueError):
            coordinator.apply(pair, review.plan, _approval(review))
        assert providers[accounts[0].id].tracks == []
        fresh = coordinator.prepare_review(pair)
        assert fresh.review_id != review.review_id
    engine.dispose()


def test_pair_lease_serializes_workers(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'lease.db').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as setup:
        source = ProviderAccount(provider_name="source", external_account_id="source")
        target = ProviderAccount(provider_name="target", external_account_id="target")
        setup.add_all((source, target))
        setup.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="source",
            target_playlist_id="target",
        )
        setup.add(pair)
        setup.commit()
        pair_id = pair.id

    with Session(engine) as first, Session(engine) as second:
        lease = acquire_pair_lease(first, pair_id)
        with pytest.raises(PairOperationBusy):
            acquire_pair_lease(second, pair_id)
        lease.release()
        replacement = acquire_pair_lease(second, pair_id)
        replacement.release()
    engine.dispose()


def test_failed_review_is_throttled_before_repeating_provider_searches() -> None:
    class FailingSearchProvider(InMemoryProvider):
        def search_track(self, track: ProviderTrack) -> ProviderTrack:
            self.search_calls += 1
            raise ProviderUnavailable("provider unavailable")

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="source", external_account_id="source")
        target = ProviderAccount(provider_name="target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="source:playlist",
            target_playlist_id="target:playlist",
        )
        session.add(pair)
        session.commit()
        providers = {
            source.id: InMemoryProvider(
                "source", "source:playlist", [ProviderTrack("source:one", "One", ("A",))]
            ),
            target.id: FailingSearchProvider("target", "target:playlist", []),
        }
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )
        with pytest.raises(ProviderUnavailable):
            coordinator.prepare_review(pair)
        assert providers[target.id].search_calls == 1
        assert SyncRunRepository(session).recent(1)[0].status == "review_failed"
        with pytest.raises(PairOperationBusy, match="wait two minutes"):
            coordinator.prepare_review(pair)
        assert providers[target.id].search_calls == 1
    engine.dispose()


def test_authorization_failure_can_be_retried_without_quota_cooldown() -> None:
    class AuthorizationFailingProvider(InMemoryProvider):
        def get_playlist(self, playlist_id: str) -> ProviderPlaylist:
            raise AuthorizationRequired("Spotify needs authorization")

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="source", external_account_id="source")
        target = ProviderAccount(provider_name="target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="source:playlist",
            target_playlist_id="target:playlist",
        )
        session.add(pair)
        session.commit()
        providers = {
            source.id: AuthorizationFailingProvider("source", "source:playlist", []),
            target.id: InMemoryProvider("target", "target:playlist", []),
        }
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )
        with pytest.raises(AuthorizationRequired):
            coordinator.prepare_review(pair)
        assert SyncRunRepository(session).recent(1)[0].status == "authorization_required"
        with pytest.raises(AuthorizationRequired):
            coordinator.prepare_review(pair)
    engine.dispose()


def test_legacy_authorization_failure_does_not_enforce_a_review_cooldown() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="source", external_account_id="source")
        target = ProviderAccount(provider_name="target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="source:playlist",
            target_playlist_id="target:playlist",
        )
        session.add(pair)
        session.flush()
        run = SyncRunRepository(session).start(pair_id=pair.id)
        SyncRunRepository(session).finish(
            run,
            "review_failed",
            '{"error_type":"AuthorizationRequired"}',
        )
        session.commit()
        providers = {
            source.id: InMemoryProvider("source", "source:playlist", []),
            target.id: InMemoryProvider("target", "target:playlist", []),
        }
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )

        review = coordinator.prepare_review(pair)

        assert review.review_id != run.id
    engine.dispose()


def test_changing_sync_direction_requires_fresh_automatic_sync_consent() -> None:
    pair = SyncPair(
        source_account_id=1,
        target_account_id=2,
        source_playlist_id="source:playlist",
        target_playlist_id="target:playlist",
    )
    pair.id = 1
    pair.created_at = datetime.now(UTC)
    two_way = automation_binding(pair)
    pair.sync_mode = "source_to_target"

    assert automation_binding(pair) != two_way


def test_pair_scoped_mapping_does_not_reclassify_another_pair() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="source", external_account_id="source")
        target = ProviderAccount(provider_name="target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        first = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="one",
            target_playlist_id="one-target",
        )
        second = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="two",
            target_playlist_id="two-target",
        )
        session.add_all((first, second))
        session.flush()
        session.add(
            ProviderTrackMapping(
                pair_id=first.id,
                account_id=target.id,
                provider_track_id="target:item",
                canonical_key="text:first|artist",
                provenance="successful_add",
                identity_version=2,
            )
        )
        session.commit()
        state = PlaylistState(
            "target",
            "two-target",
            "Two",
            (TrackState("text:second|artist", "Second", ("Artist",), "target:item"),),
        )
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda *_: None,  # type: ignore[arg-type,return-value]
        )

        assert coordinator._apply_track_mappings(second.id, target.id, state) == state
    engine.dispose()


def test_mapping_cache_prefers_source_track_id_over_normalized_metadata() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="spotify", external_account_id="source")
        target = ProviderAccount(provider_name="youtube_music", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="spotify:source",
            target_playlist_id="youtube_music:target",
        )
        session.add(pair)
        session.flush()
        session.add(
            ProviderTrackMapping(
                pair_id=pair.id,
                account_id=target.id,
                provider_track_id="youtube_music:verified-video",
                canonical_key="text:old-title|artist",
                source_provider_track_id="spotify:exact-track",
                source_isrc="isrc-123",
                provenance="successful_add",
            )
        )
        session.commit()
        plan = ReconciliationPlan(
            actions=(
                ReconciliationAction(
                    Side.TARGET,
                    ActionType.ADD_TRACK,
                    TrackState(
                        "text:renamed-title|artist",
                        "Renamed title",
                        ("Artist",),
                        "spotify:exact-track",
                        isrc="ISRC-123",
                    ),
                    "test",
                ),
            ),
            conflicts=(),
        )
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda *_: None,  # type: ignore[arg-type,return-value]
        )

        cached = coordinator._cached_resolutions(pair, plan)

        assert cached[0].provider_track_id == "youtube_music:verified-video"
    engine.dispose()


def test_durable_search_cache_avoids_repeating_an_unapplied_catalogue_lookup() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="source", external_account_id="source")
        target = ProviderAccount(provider_name="target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="source:playlist",
            target_playlist_id="target:playlist",
        )
        session.add(pair)
        session.commit()
        source_provider = InMemoryProvider(
            "source", "source:playlist", [ProviderTrack("source:one", "One", ("Artist",))]
        )
        target_provider = InMemoryProvider("target", "target:playlist", [])
        plan = ReconciliationPlan(
            actions=(
                ReconciliationAction(
                    Side.TARGET,
                    ActionType.ADD_TRACK,
                    TrackState("text:one|artist", "One", ("Artist",), "source:one"),
                    "test",
                ),
            ),
            conflicts=(),
        )
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: source_provider if account.id == source.id else target_provider,
        )

        coordinator._resolve_additions(pair, plan, source_provider, target_provider)
        session.commit()
        coordinator._resolve_additions(pair, plan, source_provider, target_provider)

        assert target_provider.search_calls == 1
        assert session.scalar(select(ProviderSearchCache)) is not None
    engine.dispose()


def test_rejected_mapped_track_is_invalidated_before_the_next_review() -> None:
    class RejectingProvider(InMemoryProvider):
        def add_tracks(self, playlist_id: str, tracks: Sequence[ProviderTrack]) -> str:
            del playlist_id, tracks
            raise TrackUnavailable("unavailable")

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = ProviderAccount(provider_name="source", external_account_id="source")
        target = ProviderAccount(provider_name="target", external_account_id="target")
        session.add_all((source, target))
        session.flush()
        pair = SyncPair(
            source_account_id=source.id,
            target_account_id=target.id,
            source_playlist_id="source:playlist",
            target_playlist_id="target:playlist",
        )
        session.add(pair)
        session.flush()
        session.add(
            ProviderTrackMapping(
                pair_id=pair.id,
                account_id=target.id,
                provider_track_id="target:unavailable",
                canonical_key="text:one|artist",
                source_provider_track_id="source:one",
                provenance="successful_add",
            )
        )
        session.commit()
        providers = {
            source.id: InMemoryProvider(
                "source", "source:playlist", [ProviderTrack("source:one", "One", ("Artist",))]
            ),
            target.id: RejectingProvider("target", "target:playlist", []),
        }
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode("ascii")),
            lambda account, _: providers[account.id],
        )

        review = coordinator.prepare_review(pair)
        coordinator.apply(pair, review.plan, _approval(review))

        assert (
            session.scalar(
                select(ProviderTrackMapping).where(
                    ProviderTrackMapping.pair_id == pair.id,
                    ProviderTrackMapping.account_id == target.id,
                )
            )
            is None
        )
    engine.dispose()


def test_spotify_duplicate_removal_fails_closed() -> None:
    duplicate = TrackState("text:one|artist", "One", ("Artist",), "spotify:same")
    plan = ReconciliationPlan(
        actions=(ReconciliationAction(Side.SOURCE, ActionType.REMOVE_TRACK, duplicate, "test"),),
        conflicts=(),
    )
    spotify_state = PlaylistState(
        "spotify", "spotify:list", "List", (duplicate, duplicate), "snapshot"
    )
    other_state = PlaylistState("youtube_music", "youtube:list", "List", ())
    spotify = InMemoryProvider("spotify", "spotify:list", [])
    youtube = InMemoryProvider("youtube_music", "youtube:list", [])

    with pytest.raises(AmbiguousSpotifyRemoval):
        SyncCoordinator._ensure_unambiguous_spotify_removals(
            plan, spotify_state, other_state, spotify, youtube
        )
