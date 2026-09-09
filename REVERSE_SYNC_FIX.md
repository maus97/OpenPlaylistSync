# Reverse synchronization recovery — 2026-09-08

## Confirmed causes

Pair 1 was enabled, two-way, opted into automatic additions/removals, and scheduled every 10 minutes. Reviews 263–267 detected two YouTube-to-Spotify additions: All of Me and Mercy (Acoustic). Mercy was unresolved, so the existing all-or-nothing matching guard held both additions. Run 268 then recorded an authorization failure. Both the scheduler and automatic runner permanently skipped any pair with that latest status.

Read-only provider verification demonstrated that both playlists were accessible (51 Spotify entries, 53 YouTube entries), and All of Me matched. Mercy's exact acoustic candidate scored 105 but a distinct Acoustic Guitar recording scored 98; the seven-point gap failed the eight-point ambiguity threshold. Their different ISRCs correctly prevented collapsing them as the same recording.

## Fixes

- Authorization failures now back off for one hour, then the normal guarded review/apply workflow retries. Repeated failures reset the cooldown; no tight retry loop or authorization bypass was introduced. Manual review remains available immediately.
- Spotify matching now recognizes guitar, piano and orchestral arrangement qualifiers. This separates the exact acoustic match from Acoustic Guitar while preserving a specifically requested guitar version.
- Search-decision cache version advanced to 6 so older unresolved decisions are not retained for 12 hours.
- Pair recovery text describes bounded retry rather than mandatory manual intervention.

## Verification and deployment

172 Linux tests passed, including real coordinator/scheduler recovery using mocked providers, both-direction add/remove roundtrips, no duplicate replay, recent-authorization backoff and recording-version negative tests. Ruff lint/format passed after line-ending normalization. Live read-only matching on the rebuilt image resolves both queued songs; health check passes. No manual live Apply or playlist removal was performed.

Local container uses image `open-playlist-sync:reverse-sync-20260908`. Existing volumes, loopback port 8000 and hardening were preserved. The configured scheduler will check again on its normal 10-minute interval; actual subsequent automatic writes were not observed during this verification.

Rollback container: `ops-before-reverse-sync-20260908` (stopped). Sensitive database backup: `/data/pre-reverse-sync-20260908.db` on the existing data volume. No schema migration. Stop the new container before restarting the old one; do not run both against the same volumes.

Remaining intentional behavior: any genuinely unresolved recording still holds the automatic batch for review. This patch does not silently skip tracks or advance the baseline past unresolved changes. No real credentials were rotated and no changes were committed/pushed.
