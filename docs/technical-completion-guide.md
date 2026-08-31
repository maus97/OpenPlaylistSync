# Open Playlist Sync technical completion guide

## Purpose

This guide is the implementation roadmap from the current operator-assisted
milestone to a dependable, self-hosted playlist synchronization service. It is
ordered by safety and dependency: complete the foundational data and provider
work before enabling unattended writes against real playlists.

The current application is useful for real-account testing with disposable
playlists. The priority-zero implementation items needed for a
real-account test are now present: occurrence-aware snapshots, non-destructive
first-sync choices, provider paging, Spotify token refresh, confidence-gated
lookups, and a durable action journal. Real provider testing should still begin
with disposable playlists because account access and provider-side behavior are
external dependencies.

## Current verified baseline

The repository was reviewed and verified on 31 August 2026 at commit
`e71523d`.

- The FastAPI application and Docker container start successfully.
- `/`, `/healthz`, `/settings`, and `/pairs` respond successfully.
- Ruff formatting and lint checks pass.
- All automated tests pass, including provider paging, token-refresh,
  occurrence-removal, first-sync, action-journal, and browser-form safety cases.
- Alembic reports that the models and migrations are aligned.
- The test suite covers the high-risk synthetic synchronization paths; live
  account behavior is deliberately not exercised by automated tests.
- The active Docker container is healthy and publishes the application on port
  8000.
- The development computer currently runs Python 3.14, while OPS targets Python
  3.12. CI and the Docker image use the correct target version.

## What "fully working" means

OPS is ready for general use when an operator can perform the following
workflow without a command line:

1. Open the GUI, configure both provider applications, and connect accounts.
2. Select a Spotify playlist and a YouTube Music playlist.
3. Choose an explicit first-sync policy and preview its exact effects.
4. Safely converge the playlists without deleting tracks on the first run.
5. Detect later changes on either service relative to the last successful
   shared baseline.
6. Match tracks accurately, show ambiguous matches for review, and preserve
   duplicates and ordering according to the selected policy.
7. Refresh credentials, handle pagination and rate limits, and recover from
   partial provider failures.
8. Schedule synchronization while requiring explicit approval for destructive
   changes.
9. Explain every run and action in the GUI.
10. Survive restart, upgrade, backup, and restore without losing credentials or
    synchronization history.

## Completion priorities

| Priority | Capability | Current state | Completion condition |
| --- | --- | --- | --- |
| P0 | Cross-provider track identity | Verified destination IDs are now saved with their source canonical key after a successful add | Add confidence thresholds and a manual candidate picker for ambiguous matches |
| P0 | First synchronization | The current baseline action accepts two potentially different playlists without converging them | The operator selects merge-only, Spotify-authoritative, YouTube-authoritative, or accept-as-is; the preview is explicit and the default never deletes |
| P0 | YouTube Music removal | The official API identifies each occurrence with a playlist-item ID | Snapshots retain playlist-item IDs and deletion tests prove the correct duplicate occurrence is removed |
| P0 | Spotify token lifecycle | Tokens refresh before expiry and rotated credentials are saved; catalogue search also requires `user-read-private` scope | Add an account-health indicator and proactive reconnect warning |
| P0 | Pagination and duplicates | Provider reads can stop at the first page and the domain collapses duplicate tracks into a dictionary | All pages are read, occurrences remain distinct, and large/duplicate playlists have synthetic tests |
| P0 | Durable execution | A later provider failure can leave earlier writes applied without a resumable action record | Every action is journaled, postconditions are checked, partial runs are recoverable, and the baseline advances only after verified convergence |
| P0 | Provider contract coverage | Critical adapter paths have limited or no tests | HTTP/client fakes cover reads, writes, paging, refresh, rate limits, malformed responses, and failures for both providers |
| P1 | Retry and rate-limit handling | No common retry policy | Bounded exponential backoff honors provider retry guidance and never retries unsafe writes blindly |
| P1 | Scheduler policy | Scheduler creates previews only | Per-pair scheduling can auto-apply explicitly permitted additions; destructive plans remain queued for approval |
| P1 | GUI completeness | Basic setup, pairing, previews, and history exist | Account health, reconnect/disconnect, pair edit/disable/delete, match review, actionable errors, and recovery are available in the GUI |
| P1 | Network security | The service has no operator authentication or CSRF protection and Compose publishes to the host network | Local-only defaults are documented; remote/TrueNAS access uses authenticated HTTPS, secure cookies, and CSRF protection |
| P1 | Backup and restore | Persistent Docker storage exists but has no tested recovery procedure | Database and generated secret files are backed up together and a clean-host restore drill passes |
| P2 | Production operations | Basic health check and logs exist | Structured redacted logs, readiness checks, migration/upgrade procedures, retention, and release rollback are documented and tested |
| P2 | TrueNAS release validation | Deployment is portable in design | Dataset permissions, upgrades, health checks, backup, and restore pass on a supported TrueNAS SCALE release |

## Phase 0: preserve the safety boundary

Do this before extending provider behavior.

1. Add `OPS_REAL_WRITES_ENABLED`, defaulting to `false` for development builds.
2. Disable real-provider Apply buttons when the flag is false and show a clear
   experimental-state message. Keep production writes restricted to explicit
   operator actions.
3. Keep plan creation read-only. Never make provider calls that change state
   while calculating or displaying a plan.
4. Continue to reject stale plans by fetching both playlists immediately before
   execution.
5. Keep the exact confirmation phrase for plans containing deletion, replacement,
   or reorder actions.

Exit criterion: no incomplete feature or provider error can silently turn a
preview into a real write.

## Phase 1: replace track keys with a canonical matching model

This is the most important architecture change. A Spotify item can have an
ISRC while the corresponding YouTube Music result commonly has only title,
artist, album, and duration. Using `isrc:...` on one side and `text:...` on the
other prevents reliable three-way comparison.

### Domain records

Add records equivalent to these concepts through an Alembic migration:

- `track_occurrences`: provider account, playlist, provider track ID,
  provider occurrence ID, position, raw metadata, availability, and snapshot.
- `canonical_tracks`: internal stable identity and normalized metadata.
- `provider_track_mappings`: canonical track, provider, provider track ID,
  evidence, confidence, source, and confirmation status.
- `match_decisions`: candidate set and an operator decision for ambiguous or
  rejected matches.

An occurrence is not the same thing as a song. Two identical songs in one
playlist must remain two occurrences. On YouTube Music, preserve the YouTube
Data API playlist-item ID as the occurrence identifier because it is required
to remove one exact playlist item.

### Matching pipeline

Use a deterministic pipeline:

1. Reuse a previously confirmed provider mapping.
2. Match exact ISRC when both providers supply it.
3. Normalize title, primary artists, featured artists, version markers, and
   duration.
4. Search the destination provider and score multiple candidates.
5. Auto-select only above a documented confidence threshold and only when the
   winning candidate is sufficiently better than the runner-up.
6. Send uncertain, unavailable, live/remix, clean/explicit, and duration-mismatch
   cases to the GUI review queue.
7. Save accepted decisions so later runs are deterministic and do not search
   again.

Do not blindly select the first provider search result.

### Required tests

Use synthetic cases for exact ISRC, punctuation and case changes, artist order,
featured artists, remasters, live tracks, clean/explicit versions, duration
differences, unavailable tracks, duplicate occurrences, ambiguous candidates,
and a saved manual decision.

Exit criterion: repeated runs produce the same canonical identity without
creating duplicates or changing an operator-confirmed mapping.

## Phase 2: make the initial synchronization explicit

The current first run stores both current snapshots as the baseline. If they
already differ, those differences are accepted and may never be synchronized.
Replace that behavior with a setup wizard offering:

- **Merge both playlists (recommended):** add the union to both sides; perform
  no deletion.
- **Spotify is authoritative:** add missing Spotify tracks to YouTube Music;
  show extra YouTube tracks but do not delete them on the initial run.
- **YouTube Music is authoritative:** the reverse of the previous option.
- **Accept current state:** record the unequal lists as intentionally accepted
  only after a clear explanation.

The preview must display matched items, additions, unavailable items, ambiguous
matches, and anything left unchanged. After applying additions, fetch both
playlists again and create the first successful baseline from the verified
result—not from the pre-write snapshots.

Exit criterion: the default first run is non-destructive and produces a useful
shared baseline from which later additions and removals can be distinguished.

## Phase 3: complete both provider adapters

### Spotify

1. Add a credential manager that checks expiry before every API operation.
2. Refresh access tokens and atomically persist any returned token changes.
   Preserve the previous refresh token when Spotify does not return a new one.
3. If refresh fails with revoked or invalid authorization, mark the account as
   requiring reconnection and guide the user from the Settings page.
4. Read every page of playlists and playlist items. Do not assume the embedded
   first page represents the complete playlist.
5. Update write endpoints and payloads against the current Spotify Web API.
6. Capture snapshot IDs where available and use them in stale-plan validation.
7. Handle null, local, unavailable, and non-track playlist items explicitly.
8. Split large writes into provider-supported batches while preserving action
   journal entries and postcondition checks.

Spotify access tokens are short-lived, so refresh handling is a normal runtime
path rather than an optional enhancement. Development-mode limits and account
allowlisting must also be explained in the GUI setup guide.

### YouTube Music

1. Use the official YouTube Data API v3 for playlist discovery, item reads,
   video metadata, search, creation, additions, and deletions.
2. Store the `videoId` and playlist-item `id` for every occurrence; delete by
   playlist-item ID and test duplicate removals explicitly.
3. Follow all `nextPageToken` values for playlists and playlist items.
4. Validate mutation responses rather than treating any returned object as
   success.
5. Keep HTTP clients injectable and test the provider with synthetic API
   responses, including auth, quota, paging, and write failures.
6. Refresh Google OAuth tokens before they expire and provide an in-GUI
   reconnect flow when refresh fails.

### Shared provider behavior

Define typed failures such as `AuthorizationRequired`, `RateLimited`,
`ProviderUnavailable`, `PlaylistChanged`, `TrackUnavailable`, and
`PermanentProviderError`. The coordinator should decide whether to retry,
pause, request review, or fail; templates should never need to parse raw
provider exceptions.

Exit criterion: fake-client contract suites prove complete reads and exact
writes, including paging, duplicate occurrences, refresh, retries, and error
translation.

## Phase 4: make write execution recoverable

Provider APIs do not offer a transaction spanning Spotify and YouTube Music.
OPS therefore needs a durable execution journal.

1. Extend `sync_runs` with pair ID, plan fingerprint, source and target
   versions, status, failure category, and resumability.
2. Add `sync_actions` with stable action IDs, provider, operation, payload hash,
   precondition, status, attempts, timestamps, and redacted result summary.
3. Acquire a per-pair lock before the final preflight. Run only one write
   execution per pair.
4. Re-fetch both playlists, verify versions and the plan fingerprint, then
   persist the planned action journal before the first provider write.
5. Execute one action at a time and record its result immediately.
6. For ambiguous network failures, re-read provider state before retrying. Do
   not assume the write failed merely because the response was lost.
7. Re-fetch both playlists after execution and verify the planned postconditions.
8. Advance the shared baseline and mark the run successful in one local database
   transaction only after those postconditions pass.
9. If execution stops halfway, show completed and pending actions in the GUI and
   provide a safe re-plan or resume path.

Exit criterion: failure injected after every possible action boundary never
advances an invalid baseline, and retrying cannot add a second unintended copy.

## Phase 5: finish the no-command-line GUI

All routine operator configuration should remain available in the browser.

### Settings and accounts

- Show Not configured, Ready to connect, Connected, Expiring, Reconnect needed,
  and Provider unavailable states.
- Add Test configuration, Connect, Reconnect, and Disconnect actions.
- Never display stored client secrets, access tokens, refresh tokens, encryption
  keys, or session secrets.
- Explain redirect URLs, Spotify allowlisting, Google device authorization, and
  requested read/write access beside the relevant fields.
- Replace any remaining text that tells normal operators to edit `.env`; keep
  environment variables documented only as deployment overrides.

### Playlist pairs

- Create, edit, disable, re-enable, and delete a pair.
- Select the first-sync policy and later conflict policy.
- Display playlist owner, visibility, item count, last read time, and account.
- Prevent the same provider playlist from being placed in conflicting active
  pairs unless the operator explicitly resolves the overlap.

### Review and recovery

- Show a plain-language summary before technical details.
- Separate additions, deletions, reorders, conflicts, unavailable tracks, and
  uncertain matches.
- Provide a candidate picker for uncertain track matches and remember the
  decision.
- Explain provider errors and give a next action instead of silently returning
  an empty playlist list.
- Show partial-run recovery, baseline age, next scheduled run, and last
  successful synchronization.

Exit criterion: a non-technical operator can configure, connect, pair, preview,
approve, diagnose, and recover without opening a shell.

## Phase 6: add scheduling without weakening safety

Use a conservative three-level policy per pair:

1. **Preview only:** scheduled runs refresh state and create a reviewable plan.
2. **Auto-apply additions:** apply only unambiguous, non-destructive additions;
   queue all other changes.
3. **Operator-approved destructive actions:** deletions, replacements, and
   reorders always require a current preview and explicit approval.

Add jitter, bounded backoff, per-pair locking, missed-run handling, and a clear
next-run display. APScheduler must run in exactly one application process. If
OPS later supports multiple replicas, move scheduling and locking to a durable
single-leader design first.

Exit criterion: restart and overlapping schedule tests cannot launch concurrent
writes for one pair, and no schedule can bypass a destructive-action guard.

## Phase 7: define and implement the deployment security model

The current service is appropriate only on the local computer or a trusted
private network. Before exposing it through TrueNAS or a reverse proxy:

1. Add operator authentication, or require and document an authenticated
   reverse proxy as a hard deployment prerequisite.
2. Add CSRF tokens to every state-changing form.
3. Use HTTPS, secure cookies, appropriate SameSite settings, trusted host
   validation, and explicit proxy-header configuration.
4. Rate-limit login, OAuth initiation, callbacks, and destructive approvals.
5. Redact credentials, OAuth codes, tokens, cookies, playlist contents, and
   provider response bodies from logs.
6. Document the threat model. The generated encryption key is separate from
   SQLite, but both currently live in the same persistent data directory; a
   compromise of that complete volume can expose credentials.
7. Add a key-rotation procedure that re-encrypts credentials atomically and
   has a tested rollback path.

Exit criterion: the selected local-only or authenticated-remote security model
is explicit, tested, and visible in deployment documentation.

## Phase 8: backup, restore, Docker, and TrueNAS

The persistent unit is the complete `/data` directory, including SQLite,
`.ops-credential-key`, and `.ops-session-secret`. Backing up only the database
can leave encrypted credentials unrecoverable.

1. Use a TrueNAS dataset or Docker volume with ownership compatible with the
   image's non-root user.
2. Create a consistent backup by stopping writes or using SQLite's supported
   backup mechanism; capture the database and generated secret files together.
3. Restore into a clean instance, run migrations, reconnect only if required,
   and verify provider accounts, pairs, baselines, and run history.
4. Document image update, database migration, rollback, and failed-upgrade
   recovery.
5. Keep one application replica while using SQLite and in-process scheduling.
6. Enable SQLite WAL mode and a suitable busy timeout only after concurrency
   tests confirm the chosen settings.
7. Validate the Compose configuration as a TrueNAS custom app without adding
   TrueNAS-only imports or runtime dependencies.

Exit criterion: a backup from one installation restores successfully on a clean
host and the next synchronization begins from the correct baseline.

## Phase 9: validation and release gates

### Automated test matrix

- Keep reconciliation and destructive-safety logic at 100% branch coverage.
- Raise coordinator and provider-adapter coverage to at least 90% with fakes.
- Cover migrations from every released schema, encrypted configuration,
  credential rotation, API forms, CSRF, scheduler locking, and backup metadata.
- Test empty, one-item, 50-item, 51-item, large, duplicate-heavy, reordered,
  unavailable, and partially matched playlists.
- Inject 401, 403, 404, 409, 429, 500, timeout, lost-response, malformed-response,
  and mid-plan failures.
- Add an end-to-end synthetic test that creates a pair, establishes an initial
  merge baseline, changes each side, reviews a plan, applies it, and proves the
  next plan is empty.
- Run on Python 3.12 in CI and keep the Docker image build as a required check.

### Live-provider acceptance test

Use separate disposable playlists and accounts where possible:

1. Connect each service through the GUI and confirm reconnect behavior.
2. Test empty and populated first-sync merges.
3. Add one unique track on each side and verify bidirectional convergence.
4. Test an ambiguous search and confirm it pauses for review.
5. Test duplicates and remove only the selected occurrence.
6. Test more than one provider page of items.
7. Expire or revoke authorization and confirm the GUI recovery path.
8. Simulate a rate limit and provider outage.
9. Interrupt a write run and verify safe recovery.
10. Back up, restore to a clean instance, and confirm the next plan is correct.

Do not use valued playlists until all priority-zero acceptance tests pass.

## Recommended implementation tickets

Work through these in order. Each ticket should include migrations, tests,
operator-facing behavior, and documentation where applicable.

1. Add the real-write feature flag and experimental UI warning.
2. Introduce occurrence-aware snapshots and preserve YouTube `setVideoId`.
3. Add canonical track mappings and the confidence-based matching service.
4. Replace initial baseline creation with the explicit first-sync wizard.
5. Implement Spotify refresh-token persistence and account-health states.
6. Complete pagination and current endpoint handling for both providers.
7. Add the durable run/action journal and partial-run recovery.
8. Build complete fake-provider contract and coordinator failure tests.
9. Add GUI account recovery, pair management, and match review.
10. Add conservative scheduled application and per-pair locking.
11. Add authentication, CSRF, secure reverse-proxy deployment, and log redaction.
12. Publish and prove backup/restore, upgrade/rollback, and TrueNAS procedures.

## Architecture decisions to record

Create short Architecture Decision Records before implementing the related
phase:

- **ADR-001 — Initial convergence policy:** default merge behavior and meaning
  of accept-as-is.
- **ADR-002 — Canonical track identity:** matching evidence, score thresholds,
  manual decisions, unavailable tracks, and mapping portability.
- **ADR-003 — Duplicate and ordering semantics:** whether ordering is synchronized
  and how occurrences are represented and removed.
- **ADR-004 — Automated write policy:** which actions scheduling can apply and
  which always require approval.
- **ADR-005 — Partial-write recovery:** idempotency, compensation, re-plan, and
  resume rules.
- **ADR-006 — Deployment threat model:** local-only default, authentication,
  reverse proxy, TLS, and secret storage expectations.
- **ADR-007 — YouTube Music integration boundary:** official YouTube Data API
  coverage, quota policy, and the limitations of playlists visible through the
  Google API.
- **ADR-008 — Persistence scale:** JSON snapshots versus normalized occurrence
  tables, retention, and expected playlist limits.

## Definition-of-done checklist

OPS can be described as fully working only when all of the following are true:

- [ ] Both accounts can be configured, connected, refreshed, disconnected, and
  recovered entirely in the GUI.
- [ ] Complete playlists are read across all pages.
- [ ] Canonical matching is deterministic and ambiguous matches require review.
- [ ] Duplicate occurrences and provider occurrence IDs are preserved.
- [ ] Every first-sync mode is explicit, previewed, tested, and non-destructive
  by default.
- [ ] Three-way reconciliation uses only the last verified successful baseline.
- [ ] Every provider write is journaled and has a checked postcondition.
- [ ] Partial failures are visible and safely resumable or re-plannable.
- [ ] Destructive actions cannot run from a schedule or stale plan without
  explicit approval.
- [ ] Provider contract, coordinator, reconciliation, and safety tests meet the
  release coverage gates.
- [ ] Remote access is authenticated and protected by HTTPS and CSRF controls,
  or the service is explicitly restricted to local access.
- [ ] Backup, clean-host restore, migration, rollback, and TrueNAS deployment
  have all been demonstrated.
- [ ] Release notes document known provider limitations and YouTube Data API
  quota/visibility constraints.

## Primary technical references

- [Spotify authorization](https://developer.spotify.com/documentation/web-api/concepts/authorization)
- [Spotify authorization code flow](https://developer.spotify.com/documentation/web-api/tutorials/code-flow)
- [Spotify token refresh](https://developer.spotify.com/documentation/web-api/tutorials/refreshing-tokens)
- [Spotify playlist item pagination](https://developer.spotify.com/documentation/web-api/reference/get-playlists-items)
- [Spotify rate limits](https://developer.spotify.com/documentation/web-api/concepts/rate-limits)
- [Spotify quota modes](https://developer.spotify.com/documentation/web-api/concepts/quota-modes)
- [Spotify February 2026 migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide)
- [ytmusicapi OAuth setup](https://ytmusicapi.readthedocs.io/en/latest/setup/oauth.html)
- [Google YouTube authorization credentials](https://developers.google.com/youtube/registering_an_application)
- [YouTube Data API v3 reference](https://developers.google.com/youtube/v3/docs)
- [Docker volume backup and restore](https://docs.docker.com/engine/storage/volumes/)
- [TrueNAS SCALE custom app deployment](https://www.truenas.com/docs/scale/26/apps/installcustomappscreens/)
