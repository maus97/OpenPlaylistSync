# Saved-match controls — 2026-09-08

The two pending YouTube-to-Spotify additions were reviewed and applied under the user's request: All of Me and Mercy - Acoustic. No removals were included. A subsequent live read confirmed 53 tracks on each service, both songs present in Spotify, and zero remaining reconciliation actions.

## User workflow

Playlists → Matches → Change version → choose an alternative → Use this version → Apply changes.

The list shows the last successful snapshot. Finish pending synchronization first. Available choices come from the provider's close-candidate search, bounded to five alternatives and excluding recordings already present at the destination. No arbitrary URL/import facility is included.

## Safety

- Replacement uses the existing persisted plan, CSRF, one-time approval, pair lease, baseline binding, provider-state revalidation, action journal and reciprocal mapping pipeline.
- Manual replacement reviews are excluded from normal review reuse, so scheduled synchronization cannot apply a user's unapproved replacement.
- The old entry is removed only after the replacement addition succeeds. Unavailable replacement errors stop execution rather than skipping to the removal.
- Duplicate occurrences, non-unique counterparts and writes to a source-controlled source are rejected.
- Post-write visibility must confirm the replacement before advancing the baseline. A delayed/partial result requires recovery; no blind rollback is attempted.
- Net-zero replacements do not synthesize an extra baseline occurrence. This has regression coverage in both directions.
- Playlist order may change: the replacement may be appended rather than inserted at the former position.

## Validation / deployment

177 Linux tests passed; Ruff lint and formatting passed. Tests include both-direction replacement, subsequent no-op reconciliation, scheduling before and after candidate choice, unavailable replacement preserving the original, read-only-source rejection, forged choices, and stale provider state. The saved-match page was opened successfully in the built-in browser. Real replacement writes were not performed; those tests used mock providers.

Image: open-playlist-sync:match-controls-20260908, live at loopback port 8000. Previous container retained stopped as ops-before-match-controls-20260908. SQLite backup /data/pre-match-controls-20260908.db contains sensitive application data. No migration or credential change. No commits or pushes.

To roll back code, stop the new container before restoring the old container name/start; both share the persistent volumes. Do not blindly restore the backup after subsequent playlist changes.
