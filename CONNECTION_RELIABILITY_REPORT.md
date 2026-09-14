# OPS connection and automatic-sync reliability

Branch: `fix/provider-connection-health` (based on main `61b7e0a`).
Assessment/deployment dates: 13–14 September 2026. No real playlist writes were
used for fault injection or test setup. The existing authorized automatic batch
was allowed to synchronize normally. Existing untracked audit checkpoints preserved.

## What stopped syncing this time

The scheduler had **not died**. Before any restart, the container had been running
26 hours, with no restarts and no active pair lock. Persistent configuration had
pair 1 enabled, two-way, automatic changes authorized, interval 10 minutes.

- Run 562 failed at **11:25:36 UTC / 21:25:36 Sydney** with generic
  `AuthorizationRequired`. Old logic imposed a one-hour wait until 12:25:36 UTC.
  It could not be superseded by successful reconnection/verification.
- Before the follow-up matching-policy change, healthy scheduler cycles at
  10:25/35/45/55 and 11:05/15 UTC still did not Apply:
  each produced **36 additions, no deletions, two unmatched recordings**:
  “Girlfriend” (Avril Lavigne) and “You Found Me” (The Fray). The existing policy
  deliberately requires match review before applying that whole batch. The UI
  did not explain this scheduler hold. This is separate from authorization.
- Latest stored applied run before deployment: 362, 9 September 08:50 UTC.
  The historical schema did not distinguish manual/automatic origin, so that
  cannot honestly be described as the last successful *automatic* Apply.
- The old API did not expose APScheduler's next timer; the observed ten-minute
  cadence indicated the next wake at 11:35:34 UTC, skipped by the auth gate.
  New diagnostics expose the actual timer rather than requiring this inference.

Old failure records lack provider identity, so the provider responsible for run
562 cannot be retrospectively proved. Spotify successfully refreshed automatically
during live verification of the fix; neither service needed a new OAuth approval.

## Root causes and implementation

| Component | Change |
| --- | --- |
| `providers/health.py`, models, migration 0013 | Persistent account verification and provider-scoped incidents separate from immutable run history. Successful authenticated operations resolve older auth incidents only for that account. Public catalogue search does not prove authentication. Legacy generic auth failures require both bound accounts verified afterwards. |
| `providers/errors.py`, Spotify/YouTube adapters | Central categories: authentication, permissions, read-only/forbidden writes, rate limit/quota, network, temporary provider, missing resource, unavailable recording, internal error. Structured HTTP status/reason/Retry-After preferred; text fallback only where the SDK discards structure. Safe allowlisted diagnostics, no raw token responses. |
| `auth/credentials.py`, OAuth services | Shared proactive refresh with five-minute margin; rejected HTTP401 retried once after refresh. Rotated refresh tokens preserved encrypted. Durable refresh lease and ciphertext compare-and-swap protect concurrent reconnects. HTTP20-second refresh timeout; invalid/revoked credentials distinguished from temporary failure. |
| `sync/automatic.py`, `sync/scheduling.py` | Category-specific retry gates. Recovery removes obsolete auth waits immediately; next normal tick can run. Every evaluated pair records attempt/outcome/next evaluation. Viable ambiguity no longer holds an automatic batch; conflicts, bulk deletions, initial baseline and uncertain writes remain explicit safety holds, not connection failures. |
| provider matching, coordinator, Activity/Matches | Strict matching remains the first choice. When it cannot separate viable results, provider ranking selects the highest-scoring candidate and records the request, selected ID, score, alternatives, reason and provider direction. Invalid title/artist identities are rejected. A track with no viable result is skipped individually and baselined as an explicit accepted difference, so unrelated work continues and the item does not repeat forever. Activity exposes the evidence and Matches retains manual replacement. |
| `main.py` | Scheduler enumeration protected separately; each pair has an independent DB session and exception boundary. Failed transactions cannot poison subsequent pairs. Safe skip/failure logs include provider/category/operation and retry context. |
| `scheduler.py` | Recurring job survives exceptions; monotonic heartbeat, completed-cycle count, true next tick, missing/paused timer watchdog. Check-now advances the existing job instead of queueing new jobs; max_instances/coalescing/in-process exclusion remain. |
| `sync/leases.py`, executor/coordinator | Separate durable automatic-job lease spans the scheduled workflow; existing operation lease still guards manual review/Apply. Expired owners cannot renew or start another write; old releases cannot unlock a new owner. Interrupted read-only work retries; potentially unacknowledged writes require review. |
| routes, pairs/Activity templates | Active health separated from failed history. Provider-aware diagnostics/resolution in Activity; scheduler status, last automatic attempt/success/outcome and next tick on Pairs. Authenticated `/system/scheduler`; CSRF-protected check-now. `/healthz` detects local scheduler stalls without probing providers. |

Retries: auth hourly while unresolved (recovery cancels immediately); access/read-only/
missing-resource six-hour checks; quota/rate limits honour Retry-After (default hour);
network/temporary/internal incidents back off 60 seconds exponentially up to an hour,
evaluated on the normal tick. Retry eligibility does not bypass safe-review holds.

## Restart and interruption safety

All incident deadlines and account/automatic leases persist. Restart restores the
configured recurring interval from encrypted settings, not a browser request.
Abandoned leases expire after 30 minutes; refresh leases after two minutes. Cleanup
acquires the operation lease before classifying interrupted runs. A preparing run,
or consumed review without a write journal, is retryable. A journal means an external
write may have happened: it is **not** automatically replayed or hidden by advancing
the baseline. Temporary errors during Apply preflight do not create that write hold.

The watchdog restores a missing or accidentally paused timer. Ordinary provider
outages do not mark the process unhealthy. A stalled local cycle (over 45 minutes
or twice the interval, whichever is longer) or a missing heartbeat over two intervals
plus two minutes fails health. Docker's health status itself does not restart an
unhealthy container; a genuine deadlock/process problem is detectable, not disguised.
Normal handled provider/DB exceptions leave future ticks scheduled.

## Tests and security checks

- Full Linux/Python3.12 suite: **237 passed** (including the new matching,
  controlled-skip, retrospective correction and scheduler-continuation cases).
- 51 targeted connection/scheduler cases pass. Includes correct-provider resolution,
  wrong-provider non-resolution, legacy records,401refresh and final rejection,
  403/429/5xx/network distinctions, rotated refresh tokens, refresh/reconnect race,
  account-scoped renewal errors, durable backoff and expiry, preserved history,
  schema upgrade/downgrade/re-upgrade, failed DB transactions across pairs,
  interrupted runs, active/expired leases, heartbeat and real repeated APScheduler
  execution after an exception, automatic Apply/no-op success timestamps.
- Ruff format/lint, Bandit and `git diff --check` passed; no type checker is
  configured by this repository. Python3.12 environment check passed in Docker.
- pip-audit: no known vulnerabilities in hashed runtime requirements.
- Gitleaks: existing 28-commit history clean.
- Docker build and isolated fresh-state startup/health/migrations passed.
- Trivy initially reported 12 inherited OS advisories in four packages. Pinned
  patches added: gzip1.13-1+deb13u1, libpcre2-8-0 10.46-1~deb13u2,
  libsqlite3-0 3.46.1-7+deb13u2, perl-base5.40.1-6+deb13u1. Rescan:
  **zero fixable High/Critical vulnerabilities**. Python requirements unchanged.
- Windows-only POSIX file-mode assertions cannot pass on NTFS; they were not
  weakened. The complete Linux suite includes and passes those assertions.

## Live deployment and verification

Private consistent backup in Docker volume `ops-connection-health-backup-20260913`,
under `/data/rollback/data` and `/data/rollback/secrets`. Backup integrity check: ok.
Original container retained stopped as `ops-before-connection-health-20260913`.

Current container `ops-ytmusicapi-browser` keeps the exact existing data/secrets
volumes, localhost8000 binding, non-root user10001, read-only root, capability drop,
no-new-privileges, resource limits and restart policy. Migrations0013/0014 additive;
no credential reset, pair/baseline changes or deleted history. DB integrity: ok.

11:49 UTC read verification: Spotify91 tracks and YouTube57 tracks, both healthy;
Spotify renewal succeeded without reconnect. Pair enabled/two-way/automatic10min
unchanged. Active connection-health warning absent in the backend health model;
authorization retry gate false. Old run562 still authorization_required as history.

Post-deployment unattended cycles were observed through **08:38:47 UTC on
14 September 2026**. At 08:38:44 UTC the scheduler selected pair 1, prepared run
687, and deliberately held its 43-change plan because source indices 7 and 42
still need recording choices: “Girlfriend” (Avril Lavigne) and “You Found Me”
(The Fray). It completed the evaluation normally at 08:38:47 UTC and persisted
the next check for **08:48:44 UTC**. Runs 682–687 show the same single, orderly
ten-minute cadence; no run is left active, no pair/automatic lock remains, no
provider incident is unresolved, and the container is healthy with zero restarts.

The live Pairs page independently displayed both services as connected and
verified, `Scheduler: waiting`, the 08:38 completed cycle, the 08:48 next check,
and `Review needed — manual matching or conflict review required`. Activity showed
the completed scheduler runs. Run 562 remains stored as historical
`authorization_required`; it no longer drives current connection health or retry
eligibility. The inspected scheduler logs contain the explicit hold and next-check
reason, no uncaught exception, and no token/credential markers.

## Follow-up: non-blocking candidate selection

The production policy now distinguishes ambiguity from invalidity. A plausible
highest-ranked result is selected even when another candidate has a close score;
the choice is auditable and correctable later. A zero/failed title or artist
identity remains ineligible. If no candidate is viable, only that track is skipped
and recorded while the rest of the batch proceeds. The search-cache algorithm was
advanced to version 7 so prior unresolved decisions are reconsidered without
changing the database schema.

The first live cycle using this policy started at **09:34:13 UTC on 14 September
2026** and completed at **09:35:31 UTC**. It applied all **43** authorized actions,
skipped zero and advanced the baseline. It chose:

- `Girlfriend` by Avril Lavigne → YouTube Music `g0TiuFwX0r8`, score 103;
- `You Found Me` by The Fray → YouTube Music `_tdWkuyFI6c`, score 108.

Both choices retained the next-best alternatives and reason in Activity history.
The operation and automatic leases were released, no provider incident or active
run remained, and the scheduler recorded the outcome `applied; 2 best-available
matches recorded`. A current SQLite backup was created inside the private data
volume before deployment as `/data/pre-best-available-match-20260914.db`.

After the final UI image was installed, its own natural scheduler cycle started at
**09:46:47 UTC** and completed at **09:46:52 UTC** with `up to date`. It re-read
both 99-track playlists, did not reopen matching review and made no duplicate
provider write. The next check is **09:56:47 UTC**. The final container is healthy,
has zero restarts, no stale lock, no active incident and no unfinished run. Its
sanitized startup/scheduler log contains no token, credential or exception output.
The authenticated live browser remained locked during final UI inspection; the
Activity evidence and Matches correction path were instead verified by the passing
rendering and end-to-end tests without entering or requesting the operator password.

## External configuration / genuinely manual actions

**Confirmed in Google Cloud:** Open Playlist Sync is External / Testing. Publish app
is disabled until Branding is completed. With OPS's YouTube scope, Google documents
seven-day refresh-token expiry in Testing: [Google OAuth token expiration](https://developers.google.com/identity/protocols/oauth2#expiration).
This is a genuine external renewal limit, not something application retries can
repair. Complete the required Google Auth Platform Branding information, then review
Audience / Publish app and any verification requirements. Reconnect YouTube after
that configuration change to obtain a newly issued token. No Cloud settings changed.

Ambiguous but viable recordings no longer require an explicit match before an
automatic batch can proceed. Review is retrospective and the saved choice remains
replaceable from Matches. Revoked grants, missing scopes, read-only targets,
conflicts, bulk-removal safeguards and uncertain prior external writes remain
legitimate reasons for human action. A host that sleeps or stops Docker also cannot
run a ten-minute background job during that downtime.

## Rollback

No environment changes are required. Additive schemas are compatible with the old
application, which ignores the extra fields. To roll back code, stop the new container
after confirming no active operation, preserve it under another name, rename the
stopped predecessor back and start it against the same volumes. Do not run both
against the same data concurrently. Prefer code-only rollback to restoring old data,
which would discard subsequent state. If schema rollback is necessary, rehearse
`alembic downgrade 0012_sync_mode` on a backup first; it removes new health diagnostics
but not old account/run data. Never overwrite the live secrets volume casually.

## Final status

Implementation, automated checks, deployment and the required post-deployment
scheduler observation are complete. No push or merge to main is part of this task.
