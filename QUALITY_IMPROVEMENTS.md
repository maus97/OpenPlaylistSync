# OPS quality and reliability improvements

Completed 2026-09-07. Existing uncommitted work was preserved; nothing was committed or pushed.

## Changes in this pass

- Reconciliation removes only occurrences actually present at the destination. Unequal baseline counts no longer repeat the same removal or manufacture removals after both sides delete a song.
- Saved approvals are bound to the pair configuration and baseline. Changing direction or accepting a newer baseline invalidates old reviews before provider writes. Approval expiry starts after preparation finishes.
- Spotify and YouTube track snapshots reject malformed, truncated, cyclic or inconsistent pagination instead of treating missing content as deletion. Missing YouTube playlists are not fabricated as empty playlists. Repeated video metadata lookups are deduplicated.
- YouTube library discovery tolerates a reported total larger than its accessible results. Observed live: 37 results, total 46, no next token. This exception applies to discovery, not playlist membership snapshots.
- Reviews retain playlist names/counts, explain zero-change outcomes more accurately and preserve the explicit initial-baseline workflow.
- Pair status now distinguishes scheduling, connection trouble and review requirements; direction changes use the same pair lock as synchronization.
- Navigation remains available on narrow screens. Loading controls reset after browser history restoration. Pair creation wording and first-sync guidance are clearer.
- YouTube setup guidance describes the actual split between public catalogue searches and official playlist operations. Compose exposes supported scheduler/body-limit settings without overriding the saved HTTPS-cookie preference when unset.

## Verification

- Full Linux suite: **167 passed**, one upstream Starlette test-client deprecation warning.
- Windows earlier run: 161 passed; two POSIX permission assertions failed on Windows. Linux subsequently verified those controls successfully.
- Ruff lint and format checks passed; Git whitespace check passed.
- Bandit: no reported issues; existing suppression-comment warnings remain.
- pip-audit, hashed runtime requirements: no known vulnerabilities found.
- Docker build and Compose validation passed.
- Isolated built-in browser: login, pair review, one synthetic acoustic-track addition, then repeat review with zero changes despite altered target metadata. No real provider writes were used for this test.
- Live browser: both provider libraries load after the discovery compatibility correction. Health endpoint passes; database migration remains at 0012_sync_mode (head).

## Deployment and rollback

Local port 8000 now runs `open-playlist-sync:quality-20260907`, container `ops-ytmusicapi-browser`. Existing data/secret volumes, loopback binding and container restrictions were preserved. This pass introduced no migration.

Previous container retained, stopped: `ops-before-quality-20260907`. SQLite online backup: `/data/pre-quality-20260907.db` in the existing data volume. Treat it as sensitive, like the primary database.

For application rollback, stop the new container, rename it aside, rename the preserved container to `ops-ytmusicapi-browser`, then start it. Both use the same persistent volumes. Do not restore the database backup blindly: it would discard later state. No schema rollback is needed for this pass.

Existing reviews without configuration bindings need a fresh review. No live review was applied during validation. Existing scheduler authorization/recovery settings were not changed; pairs already requiring connection recovery may still require a successful Review before scheduled checks resume.

## Boundaries and follow-up

This is a targeted reliability pass, not a claim that every possible package defect has been eliminated. External provider restrictions and quotas still apply. Real-account destructive, long-running scheduled and concurrent external-write tests were not performed. Potential Google device-flow pending/slow-down retry handling and application log-level wiring remain follow-up review items; they were not changed in this pass.
