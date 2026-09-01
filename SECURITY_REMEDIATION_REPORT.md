# Open Playlist Sync Security Remediation Report

## 1. Executive Summary

All 14 findings from `SECURITY_AUDIT.md` were independently confirmed and
remediated in the `security/remediation` branch. The High finding and all Medium
findings are fixed and covered by automated regression tests. The repository
contains no remaining known Critical or High dependency or container-image
vulnerability based on the completed scans.

The application is **READY AFTER MANUAL ACTIONS**. The remediated branch must be
reviewed and merged, the deployment must be backed up and upgraded, and any
network-accessible installation must be placed behind correctly configured
HTTPS. The existing live deployment was deliberately not changed by this task.

## 2. Finding Status Matrix

| ID | Original severity | Finding | Final status | Fix | Verification | Files changed |
|---|---|---|---|---|---|---|
| OPS-SEC-001 | High | Privileged UI lacked authentication on public `main` | FIXED — VERIFIED | Fail-closed local-administrator middleware and authenticated routes | Negative route tests; isolated HTTP checks | `src/ops/main.py`, `src/ops/security/middleware.py`, `src/ops/api/routes.py` |
| OPS-SEC-002 | Medium | Plain HTTP, insecure cookie, all-interface default | FIXED — REQUIRES PRODUCTION VERIFICATION | Loopback-only default publish; production Secure cookie and HSTS; trusted-proxy configuration | Cookie/header/config tests; isolated production HTTP check | `compose.yaml`, `src/ops/main.py`, `src/ops/config.py`, `docs/deployment.md` |
| OPS-SEC-003 | Medium | First network visitor could claim setup | FIXED — VERIFIED | One-time high-entropy out-of-band bootstrap token and atomic administrator creation | Missing/wrong/correct/replay/concurrency tests | `src/ops/security/bootstrap.py`, `src/ops/api/routes.py`, `templates/local_auth_setup.html` |
| OPS-SEC-004 | Medium | Race-prone throttle and global lockout DoS | FIXED — VERIFIED | Atomic source reservations, progressive throttling, bounded scrypt concurrency, larger scrypt work factor, input limits | Concurrent reservation, recovery, proxy, and request-size tests | `src/ops/security/local_auth.py`, `src/ops/security/network.py`, `src/ops/security/middleware.py` |
| OPS-SEC-005 | Medium | Review was an expensive state-changing GET | FIXED — VERIFIED | GET displays persisted data only; CSRF-protected POST prepares bounded/deduplicated reviews; attempt throttle and retention | Method, CSRF, work-limit, throttle, and retention tests | `src/ops/api/routes.py`, `src/ops/sync/coordinator.py`, `templates/pairs.html` |
| OPS-SEC-006 | Medium | Apply lacked serialization and exact one-time state binding | FIXED — VERIFIED | Database pair lease; expiring one-time token; full ordered plan/state hashes; provider re-read; atomic consumption | Replay, stale-state, concurrent lease, and partial-failure tests | `src/ops/sync/coordinator.py`, `src/ops/sync/leases.py`, `src/ops/sync/serialization.py`, migration `0009` |
| OPS-SEC-007 | Medium | Spotify duplicate deletion could not identify an occurrence safely | FIXED — VERIFIED | Snapshot-bound removal and fail-closed rejection for ambiguous duplicate URIs | Duplicate, snapshot, and provider adapter tests | `src/ops/providers/spotify.py`, `src/ops/sync/coordinator.py`, `src/ops/sync/executor.py` |
| OPS-SEC-008 | Medium | Unicode and cross-pair mapping collisions | FIXED — VERIFIED | Unicode-preserving identity v2; opaque fallback; pair/provenance-scoped mappings | Unicode corpus and pair-isolation tests; migration cycle | `src/ops/domain.py`, `src/ops/models.py`, `src/ops/storage/repositories.py`, migration `0009` |
| OPS-SEC-009 | Low | OAuth code/state leaked to access logs | FIXED — VERIFIED | Sensitive query redaction plus disabled production access log | Synthetic log regression test; runtime log inspection | `src/ops/security/logging.py`, `src/ops/entrypoint.py` |
| OPS-SEC-010 | Low | Weak YouTube identity, scope, and disconnect lifecycle | FIXED — REQUIRES PRODUCTION VERIFICATION | Official device flow, narrower `youtube.force-ssl` scope, stable channel identity, account-switch guard, pair pause/credential cleanup | Mocked OAuth, identity, switching, and disconnect tests | `src/ops/auth/youtube_music.py`, `src/ops/api/routes.py`, `src/ops/providers/youtube_music.py` |
| OPS-SEC-011 | Low | Missing security headers and trusted-host boundary | FIXED — VERIFIED | Restrictive CSP and browser headers, no-store policy, TrustedHost allowlist | Header/host integration tests and isolated runtime check | `src/ops/security/middleware.py`, `src/ops/main.py`, `static/app.js` |
| OPS-SEC-012 | Low | SQLite FK and storage/key separation weaknesses | FIXED — REQUIRES PRODUCTION VERIFICATION | Foreign keys on every connection, restrictive file modes, separate Docker secret volume, legacy-key migration | FK/file-mode tests and isolated non-root runtime | `src/ops/db.py`, `src/ops/config.py`, `compose.yaml` |
| OPS-SEC-013 | Low | Incomplete container containment and image patching | FIXED — VERIFIED | Pinned multi-stage non-root image; patched OpenSSL; read-only root; dropped caps; no-new-privileges; resource limits | Build/start/health/runtime inspection; Docker Scout and Trivy | `Dockerfile`, `compose.yaml`, `requirements.lock` |
| OPS-SEC-014 | Low | Mutable/incomplete CI and build security | FIXED — VERIFIED (repository); MANUAL GITHUB SETTINGS REQUIRED | Pinned Actions/scanners/toolchain/base; hash locks; security gates; Dependabot; policy | Actionlint, lock/build checks, local equivalents of all jobs | `.github/workflows/ci.yml`, `.github/dependabot.yml`, `SECURITY.md` |

## 3. Detailed Remediation

### OPS-SEC-001 — Authentication

The released application had no authentication boundary. The retained local
administrator implementation is now wired into the application as a fail-closed
server-side middleware. Every route other than health, static assets, login, and
the protected first-run setup boundary requires a valid signed session tied to
the current administrator session generation. Password changes invalidate all
older sessions. Tests verify privileged routes cannot be reached anonymously.

### OPS-SEC-002 and OPS-SEC-003 — Transport and bootstrap

Compose now publishes to `127.0.0.1` unless the operator explicitly changes the
bind address. Production mode defaults the session cookie to `Secure` and adds
HSTS; HTTPS, allowed host names, and trusted proxy addresses are explicit
deployment settings. First-run setup requires a randomly generated token read
from the server console. The token is stored in the secret volume, compared in
constant time, and consumed after the one permitted administrator is created.

The repository cannot itself provide TLS for an operator's hostname. HTTPS and
direct-port isolation therefore require production verification.

### OPS-SEC-004 — Password and request abuse

The root cause was non-atomic read/modify/write throttling combined with a small
whole-account lock threshold and unconstrained concurrent password hashing.
Login attempts are now atomically reserved per source in SQLite, source identity
uses forwarding headers only from configured trusted proxies, scrypt uses
`N=2^15, r=8, p=3`, verification concurrency is bounded, and oversized bodies
and passwords are rejected before hashing. Legacy hashes are upgraded after a
successful login. One source no longer locks out every legitimate source.

### OPS-SEC-005 and OPS-SEC-006 — Review and Apply

Provider work and database changes were incorrectly reachable through GET, and
approval was bound to an incomplete local fingerprint. Review preparation is
now a CSRF-protected POST with limits of 10,000 playlist items, 5,000 actions,
and 500 provider searches. Recent failed/preparing attempts are throttled, open
reviews are reused safely, and old previews are pruned.

Each prepared review persists the full ordered plan, provider item identities,
occurrences, state hashes, and snapshot identifiers. Apply holds a database
pair lease, re-fetches both providers, rejects drift, atomically consumes a
short-lived approval token, and journals each attempted action. The same review
cannot be replayed. A partial provider failure does not advance the baseline.

### OPS-SEC-007 and OPS-SEC-008 — Playlist integrity

Spotify removal now carries the reviewed snapshot and aborts if the same URI
occurs more than once, because Spotify cannot safely select a particular
duplicate occurrence through this endpoint. OPS asks for manual resolution and
a fresh review instead of guessing.

Track identity v2 preserves normalized Unicode rather than deleting non-ASCII
text, and supplies an opaque non-colliding fallback when metadata is absent.
Cross-provider mappings are pair-scoped and record identity version and
provenance, preventing one playlist pair from silently rewriting another.
Legacy v1 baselines require explicit re-baselining before destructive use.

### OPS-SEC-009 through OPS-SEC-012 — OAuth, browser, and storage

OAuth callback query values are redacted and container access logging is
disabled. Spotify uses state validation and PKCE. YouTube uses Google's official
device and token endpoints, the narrower playlist-capable scope, and the
authenticated channel ID. Reconnecting a different account is blocked until an
explicit disconnect, which pauses affected pairs and clears all local provider
credentials.

A restrictive CSP, frame denial, nosniff, conservative referrer/permissions
policies, cross-origin opener isolation, sensitive-page no-store policy, and
host allowlist are applied consistently. Inline script was moved to a static
asset so the CSP does not need `unsafe-inline`.

SQLite foreign keys are enabled on every connection with a busy timeout. Data
and secret directories/files use restrictive POSIX permissions, and Compose
stores keys separately from the database while migrating legacy keys safely.

## 4. OAuth and Authentication Changes

Spotify authorization is an authenticated CSRF-protected POST. The callback
requires a constant-time state match and a session-bound PKCE verifier. Requested
scopes remain limited to profile identity and the private/public/collaborative
playlist operations OPS implements. Spotify pagination accepts only HTTPS URLs
on Spotify's API host and expected path, closing a future SSRF boundary.

Google authorization uses the device flow, where redirect state and PKCE do not
apply. OPS requests `youtube.force-ssl`, resolves the connected channel via
`channels.list(mine=true)`, and prevents silent identity replacement. Tokens and
client secrets remain Fernet-encrypted at rest with the encryption key outside
the data volume in Docker. Provider tokens are never sent to browser code.

OPS is intentionally a single-operator application, not a multi-user service.
There is one administrator identity and no second user's objects to authorize;
all provider accounts, pairs, and synchronization endpoints are protected by
that server-side administrator boundary.

## 5. Synchronization Safety Changes

- First sync remains additive unless the operator explicitly accepts the current
  state; legacy identity data cannot authorize deletions.
- Deletions require the existing exact confirmation phrase plus the one-time,
  state-bound review approval.
- Duplicate occurrences remain represented independently; ambiguous Spotify
  deletion fails closed.
- A consumed review cannot be retried. Recovery begins with a fresh provider
  read and new review, preventing replayed writes.
- Partial failure is journaled and never advances the accepted baseline.
- Database leases serialize review, baseline acceptance, and Apply per pair.
- Exact provider-state checks prevent stale plans and sync ping-pong caused by
  applying against unseen remote edits.
- Pair-scoped mappings and explicit account-switch handling prevent one pair or
  replacement provider account from silently changing another pair's meaning.

## 6. Docker / Deployment Changes

The runtime image is built from the pinned Python 3.12.14 slim-trixie digest.
Runtime dependencies and build tools are hash locked; build tools and package
installers are absent from the final virtual environment. The application and
templates are root-owned while the process runs as UID/GID 10001.

Compose adds a read-only root filesystem, private writable volumes only for
`/data` and `/run/ops-secrets`, a noexec/nosuid/nodev `/tmp`, all-capability drop,
no-new-privileges, PID/memory/CPU limits, an init process, health check, and
loopback-only default publishing. The current patched image had zero Critical or
High vulnerabilities in both Docker Scout and Trivy.

Settings now provides an HTTPS-mode switch. The preference is encrypted with
the other GUI configuration, takes effect after restart, and controls Secure
session cookies and HSTS. An explicit `OPS_SESSION_COOKIE_SECURE` value locks
the switch and provides an administrative recovery override. TLS certificates
and termination remain the reverse proxy's responsibility.

The live deployment was not restarted or modified. Its final TLS, proxy, port,
volume, and cookie state must be checked after deployment.

## 7. Dependencies

- Upgraded `cryptography` from vulnerable 49.0.0 to 50.0.1.
- Removed the unused/private-API `ytmusicapi` dependency.
- Added exact, hash-checked runtime and build requirement exports.
- Pinned `setuptools` 84.0.0, `wheel` 0.48.0, and CI `uv` 0.12.7.
- Installed Debian OpenSSL/libssl 3.5.7 security fixes over the pinned base
  image's vulnerable 3.5.6 packages.

`pip-audit` found no known vulnerabilities in the final runtime lock.

## 8. GitHub / CI/CD Changes

The workflow retains read-only `GITHUB_TOKEN` permission and safe
`pull_request` rather than `pull_request_target`. Checkout and Python setup
Actions are pinned to commit SHAs, checkout credentials are not persisted, and
dependency installation is frozen. CI now runs formatting, lint, Bandit,
`pip-audit`, all tests, a full-history redacted Gitleaks scan, container build,
and a pinned Trivy Critical/High gate. Dependabot covers Python, Docker, and
GitHub Actions monthly. `SECURITY.md` defines a private-reporting process.

Repository branch protection, private vulnerability reporting, GitHub secret
scanning, and required checks are settings outside this checkout and remain
manual administrative actions.

## 9. Tests

- Python 3.12.14 locked environment: **69 passed**.
- Ruff 0.16.3 formatting: **passed** across 74 files.
- Ruff lint: **passed**.
- Bandit 1.9.4: **passed**, with documented narrow suppressions for empty
  sentinels, OAuth endpoint constants, fixed-argument `execv`, and the internal
  container bind.
- `pip-audit` 2.10.1: **no known vulnerabilities**.
- Gitleaks 8.30.1: **12 commits scanned; no leaks found**.
- Migration: fresh upgrade to `0009`, downgrade to `0008`, and re-upgrade to
  `0009` all passed on a disposable SQLite volume.
- Hardened Docker image: **built successfully**.
- Isolated runtime: migrations, startup, `/healthz`, non-root user, read-only
  root, dropped capabilities, no-new-privileges, resource limits, loopback port,
  and separate volumes verified.
- Isolated HTTP boundary: setup 200; unauthenticated Settings and API docs 303 to
  setup; invalid Host 400; CSP, HSTS, no-store, DENY, and nosniff present.
- Docker Scout: **0 Critical / 0 High** after patching OpenSSL.
- Trivy 0.74.0: **0 Critical / 0 High** across OS and Python packages.
- Actionlint: **passed** for the hardened GitHub workflow.
- GitHub Actions run `33459963121` for commit `eca9e00`: **quality,
  secrets, and container jobs all passed**.

Tests use mocked or disposable providers and storage. No real playlist was
created, changed, or deleted.

## 10. Remaining Risks

- The current live container has not yet been upgraded, so repository fixes are
  not evidence that its runtime is already protected.
- TLS and direct-port isolation depend on operator-controlled network and reverse
  proxy configuration. OPS deliberately does not terminate TLS itself.
- The revised Spotify and Google flows passed mocked tests but need one
  legitimate reconnect/acceptance test after deployment.
- SQLite supports one OPS application instance. Multiple replicas sharing a
  volume remain unsupported.
- Host or backup compromise that obtains both the data and secret volumes can
  recover stored OAuth credentials. Volume separation reduces accidental
  exposure but does not replace host security.
- Provider APIs cannot offer transactional rollback. OPS fails closed, journals
  partial work, and requires a fresh review, but it cannot undo a provider write
  that succeeded before a network failure.

## 11. Manual Actions Required

1. Review and merge `security/remediation` only after its GitHub checks pass.
2. Enable GitHub branch protection for `main`; require the CI quality, secrets,
   and container jobs. Enable private vulnerability reporting and secret
   scanning where the repository plan supports them.
3. Back up the existing data and secret/key material before deployment. Verify
   the exact Compose volume names first.
4. Configure an HTTPS reverse proxy or private VPN boundary. Set
   `OPS_ENVIRONMENT=production`, the exact external hostname in
   `OPS_ALLOWED_HOSTS`, and only known proxy addresses in
   `OPS_TRUSTED_PROXY_IPS`. Enable **HTTPS mode** in Settings and restart, or set
   `OPS_SESSION_COOKIE_SECURE=true` as a deployment override. Keep direct port
   8000 loopback-only or unreachable from untrusted clients.
5. Update Spotify's registered callback to the exact HTTPS URL and reconnect if
   the callback origin changes. Reconnect Google/YouTube to grant the narrower
   scope and confirm OPS displays the expected channel identity.
6. If the test administrator password previously shared outside the password
   manager is still active, change it in Settings after deployment. This action
   invalidates prior sessions.
7. Review provider account security pages and revoke obsolete OPS grants if old
   applications or credentials are no longer used. No current repository or Git
   history secret was found, so the audit evidence does not require OAuth client
   secret rotation or Git-history rewriting.

## 12. Deployment / Rollback Information

Deployment changes include migration `0009_security_remediation_state`, a new
secret volume, new security/environment settings, and the hardened image.

Recommended order:

1. Record the current image/commit and stop scheduling new reviews.
2. Stop OPS and create protected backups of the data and existing key material.
3. Configure the new secret volume and HTTPS/host/proxy environment.
4. Build or pull the reviewed remediated image and start exactly one OPS
   container. The entrypoint upgrades Alembic automatically.
5. Confirm health, `alembic current` at `0009_security_remediation_state`, login,
   cookie/header behavior through HTTPS, provider identities, pairs, and history.
6. Create a review against disposable playlists before allowing normal use.

For rollback, stop the new container, restore the pre-upgrade database and keys,
restore the previous image/commit and configuration, and start that matching
version. Do not run an older image against a database still at revision `0009`.
The migration downgrade was tested, but restoring the complete pre-upgrade
backup is the safer production rollback because provider-side writes are not
transactional with SQLite.

## 13. Final Security Status

1. **Have all Critical findings been fixed?** Yes; the audit had none, and final
   scanners report no Critical package findings.
2. **Have all High findings been fixed?** Yes, on `security/remediation`.
3. **Is it now safe to connect real Spotify accounts?** Yes after the branch is
   deployed through HTTPS and the callback/reconnect check succeeds.
4. **Is it now safe to connect real Google/YouTube accounts?** Yes after deployment
   and one reconnect verifies the narrow scope and expected channel identity.
5. **Are OAuth tokens adequately protected?** Yes against application/database-
   only disclosure: they are encrypted, browser-isolated, and query-log safe.
   Host access to both volumes remains a trusted boundary.
6. **Is playlist synchronization sufficiently protected against destructive
   errors?** Yes for the reviewed threat model: first sync is safe, Apply is
   state-bound/one-time/serialized, ambiguous deletion fails closed, and partial
   failures do not advance state.
7. **Is cross-user isolation correctly enforced?** OPS is intentionally
   single-operator; all objects are behind that one server-side identity. There
   is no supported multi-user tenancy to isolate.
8. **Is the Docker deployment appropriately hardened?** The declared and isolated
   tested deployment is. The actual live instance requires post-deploy checks.
9. **Are any secrets still exposed?** No repository or Git-history secret was
   found. Change the previously shared test password if it remains active.
10. **Is Open Playlist Sync ready for production?** **READY AFTER MANUAL ACTIONS**.

The five highest-priority remaining actions are: merge the remediation branch,
deploy it after backup, enforce HTTPS with no direct-port bypass, reconnect and
verify both providers, and require the new CI/security checks on `main`.
