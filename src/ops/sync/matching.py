"""Conservative candidate scoring used when one provider needs a track lookup."""

import re
import unicodedata
from collections.abc import Sequence

from ops.providers.types import ProviderTrack


def _normal(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def candidate_score(requested: ProviderTrack, candidate: ProviderTrack) -> float:
    """Score a candidate without guessing when core metadata disagrees."""

    score = 0.0
    if requested.isrc and candidate.isrc and requested.isrc.casefold() == candidate.isrc.casefold():
        score += 100.0
    if _normal(requested.title) == _normal(candidate.title):
        score += 60.0
    requested_artists = {_normal(item) for item in requested.artists}
    candidate_artists = {_normal(item) for item in candidate.artists}
    if requested_artists and candidate_artists:
        score += 30.0 * len(requested_artists & candidate_artists) / len(requested_artists)
    if requested.duration_ms and candidate.duration_ms:
        difference = abs(requested.duration_ms - candidate.duration_ms)
        if difference <= 2_500:
            score += 10.0
        elif difference > 15_000:
            score -= 30.0
    return score


def choose_best_candidate(
    requested: ProviderTrack, candidates: Sequence[ProviderTrack]
) -> ProviderTrack | None:
    """Return only a clearly better high-confidence candidate.

    Returning ``None`` sends an uncertain real-provider addition back to review
    instead of silently inserting the first search result.
    """

    scored = sorted(
        ((candidate_score(requested, candidate), candidate) for candidate in candidates),
        key=lambda item: item[0],
        reverse=True,
    )
    if not scored or scored[0][0] < 75:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 10:
        return None
    return scored[0][1]
