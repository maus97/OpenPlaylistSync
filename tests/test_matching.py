from ops.providers.types import ProviderTrack
from ops.sync.matching import choose_best_candidate


def test_matcher_prefers_exact_title_artist_and_duration() -> None:
    requested = ProviderTrack("spotify:one", "Song", ("Artist",), duration_ms=180_000)
    exact = ProviderTrack("youtube:one", "Song", ("Artist",), duration_ms=181_000)
    unrelated = ProviderTrack("youtube:two", "Song", ("Other",), duration_ms=240_000)

    assert choose_best_candidate(requested, (unrelated, exact)) == exact


def test_matcher_rejects_ambiguous_or_low_confidence_results() -> None:
    requested = ProviderTrack("spotify:one", "Song", ("Artist",))
    equally_good = (
        ProviderTrack("youtube:one", "Song", ("Artist",)),
        ProviderTrack("youtube:two", "Song", ("Artist",)),
    )

    assert choose_best_candidate(requested, equally_good) is None
    assert (
        choose_best_candidate(requested, (ProviderTrack("youtube:x", "Else", ("Other",)),)) is None
    )
