"""Provider-neutral fallback scoring used by simple or synthetic providers."""

import re
import unicodedata
from collections.abc import Sequence

from ops.providers.types import AutomaticCandidateMatch, ProviderTrack, ScoredCandidate


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
    """Return the highest-ranked viable result, even when another is close."""

    scored = sorted(
        ((candidate_score(requested, candidate), candidate) for candidate in candidates),
        key=lambda item: item[0],
        reverse=True,
    )
    if not scored or scored[0][0] < 75:
        return None
    return scored[0][1]


def best_available_match(
    requested: ProviderTrack, candidates: Sequence[ProviderTrack]
) -> AutomaticCandidateMatch | None:
    """Select a plausible fallback while rejecting weak metadata-only guesses."""

    scored = sorted(
        ((candidate_score(requested, candidate), candidate) for candidate in candidates),
        key=lambda item: (item[0], item[1].provider_track_id),
        reverse=True,
    )
    viable = tuple(ScoredCandidate(candidate, score) for score, candidate in scored if score >= 75)
    if not viable:
        return None
    return AutomaticCandidateMatch(
        selected=viable[0].track,
        score=viable[0].score,
        alternatives=viable[1:],
        reason=(
            "top candidates had similar provider matching scores"
            if len(viable) > 1 and viable[0].score - viable[1].score < 10
            else "strict matching did not select a result; the best viable candidate was used"
        ),
    )
