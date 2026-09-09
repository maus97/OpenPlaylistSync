"""Recording identities must survive provider display metadata and retries."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ops.config import Settings
from ops.db import Base
from ops.models import ProviderAccount, SyncBaseline, SyncPair, SyncRun
from ops.providers.spotify import SpotifyProvider
from ops.providers.types import ProviderPlaylist, ProviderTrack
from ops.sync.automatic import automation_binding, run_automatic_pair
from ops.sync.coordinator import SyncCoordinator
from ops.sync.domain import ActionType, Side
from ops.sync.safety import Approval, plan_fingerprint


class PlaylistProvider:
    def __init__(self, name, tracks):
        self.name = name
        self.tracks = list(tracks)
        self.searches = 0
        self.writes = 0

    resolve_search_candidates = staticmethod(SpotifyProvider.resolve_search_candidates)

    def get_playlist(self, playlist_id):
        return ProviderPlaylist(playlist_id, "Synthetic roundtrip", tuple(self.tracks))

    def search_track(self, track):
        self.searches += 1
        return replace(track, provider_track_id=f"{self.name}:resolved-{self.searches}")

    def add_tracks(self, playlist_id, tracks):
        self.writes += len(tracks)
        self.tracks.extend(tracks)

    def remove_tracks(self, playlist_id, tracks, **kwargs):
        self.writes += len(tracks)
        for track in tracks:
            self.tracks.remove(
                next(t for t in self.tracks if t.provider_track_id == track.provider_track_id)
            )


class DelayedVisibilityProvider(PlaylistProvider):
    """Acknowledges writes before its playlist listing catches up."""

    def __init__(self, name, tracks):
        super().__init__(name, tracks)
        self.pending_tracks = []

    def add_tracks(self, playlist_id, tracks):
        self.writes += len(tracks)
        self.pending_tracks.extend(tracks)

    def reveal_pending(self):
        self.tracks.extend(self.pending_tracks)
        self.pending_tracks.clear()


@pytest.fixture
def pair_context():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        accounts = [
            ProviderAccount(provider_name=n, external_account_id=n) for n in ("source", "target")
        ]
        session.add_all(accounts)
        session.flush()
        pair = SyncPair(
            source_account_id=accounts[0].id,
            target_account_id=accounts[1].id,
            source_playlist_id="spotify:source",
            target_playlist_id="youtube_music:target",
        )
        session.add(pair)
        session.commit()
        providers = {
            a.id: PlaylistProvider(n, [])
            for a, n in zip(accounts, ("spotify", "youtube_music"), strict=True)
        }
        coordinator = SyncCoordinator(
            session,
            Settings(credential_encryption_key=Fernet.generate_key().decode()),
            lambda account, _: providers[account.id],
        )
        yield session, pair, coordinator, providers[accounts[0].id], providers[accounts[1].id]
    engine.dispose()


def apply_review(coordinator, pair, review):
    coordinator.apply(
        pair,
        review.plan,
        Approval(
            plan_fingerprint(review.plan),
            "",
            review_id=review.review_id,
            token=review.approval_token,
        ),
    )


def test_synced_track_with_changed_youtube_metadata_is_not_reimported(pair_context):
    session, pair, coordinator, spotify, youtube = pair_context
    spotify.tracks = [
        ProviderTrack("spotify:original", "Afterglow", ("Ed Sheeran",), duration_ms=185000)
    ]
    apply_review(coordinator, pair, coordinator.prepare_review(pair))
    youtube.tracks[0] = replace(
        youtube.tracks[0],
        title="Ed Sheeran - Afterglow (Official Lyric Video)",
        artists=("Ed Sheeran - Topic",),
    )
    review = coordinator.prepare_review(pair)
    assert review.plan.actions == ()
    assert len(spotify.tracks) == len(youtube.tracks) == 1
    assert spotify.writes == spotify.searches == 0
    assert youtube.writes == 1
    assert coordinator.prepare_review(pair).plan.actions == ()


@pytest.mark.parametrize("has_baseline", [False, True])
def test_existing_playlist_recording_precedes_remote_release_search(pair_context, has_baseline):
    session, pair, coordinator, spotify, youtube = pair_context
    spotify.tracks = [
        ProviderTrack("spotify:original", "Afterglow", ("Ed Sheeran",), duration_ms=185000)
    ]
    youtube.tracks = [
        ProviderTrack(
            "youtube_music:existing",
            "Ed Sheeran - Afterglow (Official Lyric Video)",
            ("Ed Sheeran",),
            duration_ms=185000,
        )
    ]
    if has_baseline:
        coordinator.accept_current_state(pair)
        youtube.tracks[0] = replace(youtube.tracks[0], title="Afterglow (Official Audio)")
    before = session.scalar(select(SyncBaseline.snapshot_json))
    review = coordinator.prepare_review(pair)
    assert review.plan.actions == ()
    assert spotify.searches == youtube.searches == 0
    assert spotify.writes == youtube.writes == 0
    assert session.scalar(select(SyncBaseline.snapshot_json)) == before


def test_acoustic_track_is_not_collapsed_into_studio_recording(pair_context):
    _, pair, coordinator, spotify, youtube = pair_context
    spotify.tracks = [
        ProviderTrack("spotify:studio", "Song", ("Artist",), duration_ms=180000, isrc="STUDIO")
    ]
    youtube.tracks = [
        ProviderTrack(
            "youtube_music:acoustic",
            "Song (Acoustic)",
            ("Artist",),
            duration_ms=160000,
            isrc="ACOUSTIC",
        )
    ]
    review = coordinator.prepare_review(pair)
    assert len(review.plan.actions) == 2
    assert {a.side for a in review.plan.actions} == {Side.SOURCE, Side.TARGET}
    assert all(a.action is ActionType.ADD_TRACK for a in review.plan.actions)


def test_mapping_changes_do_not_create_phantom_removals(pair_context):
    session, pair, coordinator, spotify, youtube = pair_context
    spotify.tracks = [
        ProviderTrack("spotify:one", "Afterglow", ("Ed Sheeran",), duration_ms=185000)
    ]
    youtube.tracks = [
        ProviderTrack(
            "youtube_music:one",
            "Ed Sheeran - Afterglow (Official Video)",
            ("Ed Sheeran",),
            duration_ms=185000,
        )
    ]
    coordinator.accept_current_state(pair)
    youtube.tracks[0] = replace(youtube.tracks[0], title="Afterglow (Official Audio)")
    review = coordinator.prepare_review(pair)
    assert review.plan.actions == ()
    # Re-evaluate the same immutable baseline after metadata changes on both sides.
    spotify.tracks[0] = replace(spotify.tracks[0], title="AFTERGLOW")
    youtube.tracks[0] = replace(youtube.tracks[0], title="Afterglow (Official Audio)")
    source, target, _, _ = coordinator._current_state(pair)
    plan, _, _ = coordinator._build_plan(pair, source, target)
    assert plan.actions == ()


def test_delayed_destination_listing_does_not_reverse_add_a_duplicate(pair_context):
    _, pair, coordinator, spotify, _ = pair_context
    youtube = DelayedVisibilityProvider("youtube_music", [])
    coordinator.provider_factory = lambda account, _: (
        spotify if account.id == pair.source_account_id else youtube
    )
    coordinator.accept_current_state(pair)
    spotify.tracks.append(
        ProviderTrack(
            "spotify:aerosmith",
            'I Don\'t Want To Miss A Thing - From "Armageddon" Soundtrack',
            ("Aerosmith",),
            isrc="USSM19801545",
        )
    )
    apply_review(coordinator, pair, coordinator.prepare_review(pair))
    # The API accepted the addition, but its first listing is still stale.
    assert len(youtube.tracks) == 0
    youtube.reveal_pending()
    review = coordinator.prepare_review(pair)
    assert review.plan.actions == ()
    assert len(spotify.tracks) == len(youtube.tracks) == 1


def test_explicit_preference_resolves_an_unknown_clean_explicit_tie(pair_context):
    _, pair, coordinator, spotify, youtube = pair_context

    class ContentPreferenceProvider(PlaylistProvider):
        resolve_explicit_preference = staticmethod(SpotifyProvider.resolve_explicit_preference)

        def search_track(self, track):
            self.searches += 1
            return None

        def close_track_candidates(self, track):
            return (
                ProviderTrack(
                    "spotify:explicit",
                    "Peaches (feat. Daniel Caesar & Giveon)",
                    ("Justin Bieber", "Daniel Caesar", "GIVĒON"),
                    album="Justice",
                    duration_ms=198_081,
                    explicit=True,
                ),
                ProviderTrack(
                    "spotify:clean",
                    "Peaches (feat. Daniel Caesar & Giveon)",
                    ("Justin Bieber", "Daniel Caesar", "GIVĒON"),
                    album="Justice",
                    duration_ms=198_081,
                    explicit=False,
                ),
            )

    preferred_spotify = ContentPreferenceProvider("spotify", [])
    coordinator.provider_factory = lambda account, _: (
        preferred_spotify if account.id == pair.source_account_id else youtube
    )
    coordinator.settings.explicit_preference = "prefer_clean"
    coordinator.accept_current_state(pair)
    youtube.tracks.append(
        ProviderTrack(
            "youtube_music:peaches",
            "Peaches",
            ("Justin Bieber - Topic",),
            duration_ms=199_000,
        )
    )

    review = coordinator.prepare_review(pair)

    assert review.unresolved_actions == ()
    assert review.plan.actions[0].side is Side.SOURCE


@pytest.mark.parametrize("origin", ["spotify", "youtube"])
def test_automatic_add_remove_roundtrip(pair_context, origin):
    _, pair, coordinator, spotify, youtube = pair_context
    for provider in (spotify, youtube):
        provider.tracks = [ProviderTrack(f"{provider.name}:keep", "Keep", ("Artist",))]
    coordinator.accept_current_state(pair)
    coordinator.settings.automatic_sync_bindings = [automation_binding(pair)]
    adding = spotify if origin == "spotify" else youtube
    adding.tracks.append(ProviderTrack(f"{adding.name}:new", "New song", ("Artist",)))
    assert run_automatic_pair(coordinator, pair) == "applied"
    assert len(spotify.tracks) == len(youtube.tracks) == 2
    adding.tracks.pop()
    assert run_automatic_pair(coordinator, pair) == "applied"
    assert len(spotify.tracks) == len(youtube.tracks) == 1
    assert run_automatic_pair(coordinator, pair) == "up to date"


def test_scheduled_sync_recovers_from_old_authorization_error(pair_context, monkeypatch):
    from contextlib import nullcontext

    from ops import main

    session, pair, coordinator, spotify, youtube = pair_context
    coordinator.accept_current_state(pair)
    coordinator.settings.automatic_sync_bindings = [automation_binding(pair)]
    coordinator.settings.scheduler_enabled = True
    youtube.tracks.append(ProviderTrack("youtube_music:new", "New", ("Artist",)))
    failed_at = datetime.now(UTC) - timedelta(hours=2)
    session.add(
        SyncRun(
            pair_id=pair.id,
            status="authorization_required",
            started_at=failed_at,
            completed_at=failed_at,
        )
    )
    session.commit()
    monkeypatch.setattr(main, "SessionLocal", lambda: nullcontext(session))
    monkeypatch.setattr(main, "load_app_settings", lambda _: coordinator.settings)
    monkeypatch.setattr(main, "SyncCoordinator", lambda *_: coordinator)
    main.run_scheduled_sync()
    assert spotify.writes == 1
    main.run_scheduled_sync()
    assert spotify.writes == 1


@pytest.mark.parametrize("side", [Side.SOURCE, Side.TARGET])
def test_manual_replacement_preserves_roundtrip_identity(pair_context, monkeypatch, side):
    _, pair, coordinator, spotify, youtube = pair_context
    for provider in (spotify, youtube):
        provider.tracks = [ProviderTrack(f"{provider.name}:old", "Song", ("Artist",))]
    coordinator.accept_current_state(pair)
    provider = spotify if side is Side.SOURCE else youtube
    replacement = ProviderTrack(f"{provider.name}:alternate", "Song - Live", ("Artist",))
    monkeypatch.setattr(provider, "close_track_candidates", lambda _: (replacement,), raising=False)
    review = coordinator.prepare_replacement(pair, side, f"{provider.name}:old")
    coordinator.settings.automatic_sync_bindings = [automation_binding(pair)]
    assert run_automatic_pair(coordinator, pair) == "up to date"
    assert spotify.writes == youtube.writes == 0
    selected = coordinator.select_candidate(
        pair, review.review_id, 0, replacement.provider_track_id
    )
    assert selected.replacement_track == replacement
    assert run_automatic_pair(coordinator, pair) == "up to date"
    assert spotify.writes == youtube.writes == 0
    apply_review(coordinator, pair, replace(selected, approval_token=review.approval_token))
    assert [t.provider_track_id for t in provider.tracks] == [replacement.provider_track_id]
    assert coordinator.prepare_review(pair).plan.actions == ()


def test_replacement_failure_keeps_original(pair_context, monkeypatch):
    from ops.providers.base import TrackUnavailable

    _, pair, coordinator, spotify, youtube = pair_context
    for provider in (spotify, youtube):
        provider.tracks = [ProviderTrack(f"{provider.name}:old", "Song", ("Artist",))]
    coordinator.accept_current_state(pair)
    replacement = ProviderTrack("spotify:alternate", "Song - Live", ("Artist",))
    monkeypatch.setattr(spotify, "close_track_candidates", lambda _: (replacement,), raising=False)
    review = coordinator.prepare_replacement(pair, Side.SOURCE, "spotify:old")
    selected = coordinator.select_candidate(
        pair, review.review_id, 0, replacement.provider_track_id
    )

    def unavailable(*args):
        raise TrackUnavailable("unavailable")

    monkeypatch.setattr(spotify, "add_tracks", unavailable)
    with pytest.raises(Exception, match="previous recording was not removed"):
        apply_review(coordinator, pair, replace(selected, approval_token=review.approval_token))
    assert [t.provider_track_id for t in spotify.tracks] == ["spotify:old"]
    assert spotify.writes == youtube.writes == 0


def test_replacement_rejects_read_only_source(pair_context):
    _, pair, coordinator, _, _ = pair_context
    pair.sync_mode = "source_to_target"
    with pytest.raises(ValueError, match="read-only"):
        coordinator.prepare_replacement(pair, Side.SOURCE, "spotify:old")


def test_replacement_rejects_unselected_and_changed_state(pair_context, monkeypatch):
    _, pair, coordinator, spotify, youtube = pair_context
    for provider in (spotify, youtube):
        provider.tracks = [ProviderTrack(f"{provider.name}:old", "Song", ("Artist",))]
    coordinator.accept_current_state(pair)
    alternate = ProviderTrack("spotify:alternate", "Song - Live", ("Artist",))
    monkeypatch.setattr(spotify, "close_track_candidates", lambda _: (alternate,), raising=False)
    review = coordinator.prepare_replacement(pair, Side.SOURCE, "spotify:old")
    with pytest.raises(ValueError, match="Choose a replacement"):
        apply_review(coordinator, pair, review)
    with pytest.raises(ValueError, match="not part of this review"):
        coordinator.select_candidate(pair, review.review_id, 0, "spotify:forged")
    selected = coordinator.select_candidate(pair, review.review_id, 0, alternate.provider_track_id)
    spotify.tracks.append(ProviderTrack("spotify:external", "External edit", ("Artist",)))
    with pytest.raises(ValueError, match="provider state changed"):
        apply_review(coordinator, pair, replace(selected, approval_token=review.approval_token))
    assert spotify.writes == youtube.writes == 0


def test_automatic_requires_pair_consent_and_baseline(pair_context):
    _, pair, coordinator, spotify, youtube = pair_context
    assert run_automatic_pair(coordinator, pair) == "disabled"
    coordinator.settings.automatic_sync_bindings = [automation_binding(pair)]
    assert run_automatic_pair(coordinator, pair) == "manual first sync required"
    pair.target_playlist_id = "youtube_music:different"
    assert run_automatic_pair(coordinator, pair) == "disabled"
    assert spotify.writes == youtube.writes == 0


def test_automatic_cannot_empty_destination(pair_context):
    _, pair, coordinator, spotify, youtube = pair_context
    for provider in (spotify, youtube):
        provider.tracks = [ProviderTrack(f"{provider.name}:keep", "Keep", ("Artist",))]
    coordinator.accept_current_state(pair)
    coordinator.settings.automatic_sync_bindings = [automation_binding(pair)]
    youtube.tracks.clear()
    assert run_automatic_pair(coordinator, pair) == "manual review required for bulk removals"
    assert len(spotify.tracks) == 1
    assert spotify.writes == youtube.writes == 0


@pytest.mark.parametrize("status", ["failed", "partially_applied", "applying"])
def test_automatic_blocks_uncertain_writes(pair_context, status):
    session, pair, coordinator, spotify, youtube = pair_context
    coordinator.accept_current_state(pair)
    coordinator.settings.automatic_sync_bindings = [automation_binding(pair)]
    session.add(SyncRun(pair_id=pair.id, status=status, started_at=datetime.now(UTC)))
    session.commit()
    assert (
        run_automatic_pair(coordinator, pair) == "manual recovery required after incomplete apply"
    )
    assert spotify.writes == youtube.writes == 0


def test_automatic_revalidates_live_state(pair_context, monkeypatch):
    _, pair, coordinator, spotify, youtube = pair_context
    coordinator.accept_current_state(pair)
    coordinator.settings.automatic_sync_bindings = [automation_binding(pair)]
    spotify.tracks.append(ProviderTrack("spotify:new", "New", ("Artist",)))
    prepare = coordinator.prepare_review

    def changed_after_review(pair):
        review = prepare(pair)
        youtube.tracks.append(ProviderTrack("youtube_music:edit", "Concurrent edit", ("Artist",)))
        return review

    monkeypatch.setattr(coordinator, "prepare_review", changed_after_review)
    with pytest.raises(ValueError, match="provider state changed"):
        run_automatic_pair(coordinator, pair)
    assert spotify.writes == youtube.writes == 0


def test_automatic_unresolved_does_not_write(pair_context, monkeypatch):
    _, pair, coordinator, spotify, youtube = pair_context
    coordinator.accept_current_state(pair)
    coordinator.settings.automatic_sync_bindings = [automation_binding(pair)]
    spotify.tracks.append(ProviderTrack("spotify:new", "New", ("Artist",)))
    monkeypatch.setattr(youtube, "search_track", lambda _: None)
    assert run_automatic_pair(coordinator, pair) == "manual matching or conflict review required"
    assert spotify.writes == youtube.writes == 0


def test_scheduled_tick_applies_only_after_enabled(pair_context, monkeypatch):
    from contextlib import nullcontext

    from ops import main

    session, pair, coordinator, spotify, youtube = pair_context
    coordinator.accept_current_state(pair)
    coordinator.settings.automatic_sync_bindings = [automation_binding(pair)]
    youtube.tracks.append(ProviderTrack("youtube_music:new", "New", ("Artist",)))
    monkeypatch.setattr(main, "SessionLocal", lambda: nullcontext(session))
    monkeypatch.setattr(main, "load_app_settings", lambda _: coordinator.settings)
    monkeypatch.setattr(main, "SyncCoordinator", lambda *_: coordinator)
    main.run_scheduled_sync()
    assert spotify.writes == 0
    coordinator.settings.scheduler_enabled = True
    main.run_scheduled_sync()
    assert spotify.writes == 1
    main.run_scheduled_sync()
    assert spotify.writes == 1
