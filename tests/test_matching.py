from ops.providers.types import ProviderTrack
from ops.sync.domain import normalize_text, track_key
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


def test_canonical_identity_preserves_unicode_and_combining_equivalence() -> None:
    first = ProviderTrack("one", "夜空中最亮的星", ("逃跑计划",))
    second = ProviderTrack("two", "平凡之路", ("朴树",))

    assert track_key(first) != track_key(second)
    assert "夜空中最亮的星" in track_key(first)
    assert normalize_text("Café") == normalize_text("Cafe\u0301")


def test_empty_metadata_uses_noncolliding_opaque_identity() -> None:
    assert track_key(ProviderTrack("one", "", ())) != track_key(ProviderTrack("two", "", ()))
