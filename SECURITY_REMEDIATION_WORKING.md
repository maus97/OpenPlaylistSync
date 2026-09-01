# Open Playlist Sync Security Remediation — Working Checkpoint

Last updated: 2026-09-01 (Australia/Sydney)

## Constraints and repository state

- Authorized implementation/remediation task.
- Branch: `security/remediation`, created from `main` at `deafdc5e7ba8ab0b407fe590f61a13037581ca75` while preserving the substantial pre-existing uncommitted working-tree changes.
- Never record credentials, token values, passwords, private keys, or other secret values here.
- Do not modify real playlists, rotate third-party credentials, reset production data, rewrite Git history, or deploy before all required validation gates pass.
- `SECURITY_AUDIT.md` is the finding source of truth. All findings must receive a final status.

## Current phase

Remediation and local validation are complete. Final report created. The remaining external steps are GitHub CI completion/review, merge, production backup/deployment, HTTPS configuration, and production/provider verification. No live deployment was changed.

## Remediation matrix

| ID | Original severity | Vulnerability | Current status | Proposed remediation | Required verification/tests | Dependencies |
|---|---|---|---|---|---|---|
| OPS-SEC-001 | High | Public `main` lacks authentication | CONFIRMED | Preserve and complete server-side local-auth middleware/routes/migrations, add negative route tests, and make the authenticated build reviewable on this branch | Full auth/API tests; unauthenticated privileged-route denial; migration test | Existing uncommitted auth implementation; all release validation |
| OPS-SEC-002 | Medium | Plain HTTP and non-Secure cookie/default all-interface exposure | CONFIRMED | Secure-by-default deployment profile, trusted HTTPS/proxy documentation and cookie policy without breaking local bootstrap/development | Config tests, response-cookie tests, Compose validation, live test stack | OPS-SEC-003; deployment design |
| OPS-SEC-003 | Medium | First network visitor can claim fresh setup | CONFIRMED | One-time high-entropy bootstrap credential and atomic administrator creation; fail closed when missing | Fresh-database negative/positive/replay/concurrency tests | Auth configuration and migration compatibility |
| OPS-SEC-004 | Medium | Race-prone throttling and global lockout DoS | CONFIRMED | Atomic attempt accounting, bounded password verification concurrency/progressive source throttling, trusted client identity and input bounds | Unit/API concurrency tests with mocked hashing; legitimate recovery tests | Proxy configuration from OPS-SEC-002 |
| OPS-SEC-005 | Medium | Review is state-changing/expensive GET | CONFIRMED | CSRF-protected POST initiation, bounded/deduplicated per-pair review and retention; GET displays only | API method/CSRF tests, deduplication/work-bound tests, compatibility tests | Pair operation lease from OPS-SEC-006 |
| OPS-SEC-006 | Medium | Apply lacks serialization, one-time approval and provider-version binding | CONFIRMED | Database-backed per-pair lease, consumable expiring approval bound to complete plan/provider state, safe retry semantics | Concurrent Apply, replay, stale-provider, partial-failure tests | Schema migration; provider snapshot work |
| OPS-SEC-007 | Medium | Spotify duplicate deletion cannot target reviewed occurrence | CONFIRMED | Preserve Spotify snapshots and fail closed/require safe reconciliation for ambiguous duplicate removals | Mocked duplicate/removal/snapshot/reorder tests | OPS-SEC-006 provider-state binding |
| OPS-SEC-008 | Medium | Unicode canonical collisions and cross-pair mapping overwrite | CONFIRMED | Unicode-preserving canonicalization; provenance/pair-safe mapping schema and migration | Unicode corpus/property tests, multi-pair isolation, migration tests | Schema migration; matching compatibility |
| OPS-SEC-009 | Low | OAuth code/state logged in access log | CONFIRMED | Callback query redaction/filter and production logging configuration | Logging regression test with synthetic secrets; no value exposure | None |
| OPS-SEC-010 | Low | YouTube identity/scope/disconnect lifecycle weak | CONFIRMED | Resolve channel identity, narrow scope if API-compatible, detect account switches, clarify/revoke lifecycle safely | Provider/OAuth mocked identity/scope/switch/disconnect tests | Schema/account compatibility; no live reconnect |
| OPS-SEC-011 | Low | Security headers and TrustedHost missing | CONFIRMED | Add tested security-header/cache/host middleware with CSP compatible with templates | Header/Host tests and UI smoke tests | HTTPS origin configuration |
| OPS-SEC-012 | Low | SQLite FK off, permissions/key separation weak | CONFIRMED | Enable FK per connection, harden POSIX paths, support external secret files/key rotation documentation | FK tests, file-mode tests, migration/backup documentation | Deployment design |
| OPS-SEC-013 | Low | Docker containment/image patching incomplete | CONFIRMED | Compatible patched dependencies/base, minimal build, drop caps, no-new-privileges, read-only root/tmpfs and resource limits | Image build, Compose config, health/start smoke, runtime inspection, image scan | Application writable-path inventory |
| OPS-SEC-014 | Low | Mutable CI/build inputs and incomplete security governance | CONFIRMED | Pin Actions/tool/base where maintainable, hashed dependency process, security workflows/update policy/security policy, restore green CI | Workflow/schema review, scanners, reproducible build, full tests | Dependency/Docker fixes |

## Work log

- Inspected initial Git status. The repository already contained extensive modified/untracked application files from earlier development; no pre-existing change was discarded.
- Created and switched to dedicated branch `security/remediation` without committing or stashing the existing work.
- Created this checkpoint before application/configuration remediation.
- Read `SECURITY_AUDIT.md` completely and re-read the audit checkpoint.
- Inspected current configuration, authentication middleware/routes, OAuth services, models/migrations, repositories, provider contracts/adapters, reconciliation/coordinator/executor, templates, Docker/Compose, CI and tests.
- Reproduced each finding from current code/configuration without touching the live deployment. None was stale or a false positive.
- Baseline validation ran in an isolated read-only-mounted Python 3.12 container: 48 tests passed. Host `uv` is unavailable, so exact-Python container validation will be retained as the authoritative test path.

## Tests and scanners

- Baseline: `pytest` on Python 3.12.14 — **48 passed**.
- Initial host Ruff/pytest commands did not run because `uv` is absent from PATH; this is an environment limitation, not a test failure.
- Final locked Python 3.12.14 suite — **69 passed**.
- Ruff format and lint — **passed**.
- Bandit — **passed** after manually validating narrow false-positive suppressions.
- `pip-audit` — **no known vulnerabilities**.
- Gitleaks 8.30.1 — **12 commits scanned; no leaks found**.
- Fresh migration, `0009` downgrade to `0008`, and re-upgrade — **passed**.
- Hardened image build and isolated runtime/HTTP boundary checks — **passed**.
- Docker Scout and Trivy 0.74.0 — **0 Critical / 0 High** after installing the patched OpenSSL/libssl packages.
- Actionlint — **passed**.
- GitHub Actions run `33459963121` for `eca9e00` — **quality, secrets, and container jobs all passed**.
- Added a GUI HTTPS-mode switch after the initial remediation run. It persists encrypted, applies Secure cookies and HSTS after restart, and can be locked or recovered with `OPS_SESSION_COOKIE_SECURE`. GitHub Actions run `33484984209` for `3e36167` passed its quality, secrets, and container jobs.

## Final finding statuses

| ID | Final status |
|---|---|
| OPS-SEC-001 | FIXED — VERIFIED |
| OPS-SEC-002 | FIXED — REQUIRES PRODUCTION VERIFICATION |
| OPS-SEC-003 | FIXED — VERIFIED |
| OPS-SEC-004 | FIXED — VERIFIED |
| OPS-SEC-005 | FIXED — VERIFIED |
| OPS-SEC-006 | FIXED — VERIFIED |
| OPS-SEC-007 | FIXED — VERIFIED |
| OPS-SEC-008 | FIXED — VERIFIED |
| OPS-SEC-009 | FIXED — VERIFIED |
| OPS-SEC-010 | FIXED — REQUIRES PRODUCTION VERIFICATION |
| OPS-SEC-011 | FIXED — VERIFIED |
| OPS-SEC-012 | FIXED — REQUIRES PRODUCTION VERIFICATION |
| OPS-SEC-013 | FIXED — VERIFIED |
| OPS-SEC-014 | FIXED — VERIFIED (repository); MANUAL GITHUB SETTINGS REQUIRED |

## Deployment considerations and manual actions

- Live deployment is not to be changed until tests, image build, migration inventory, backup/rollback notes, and all Critical/High statuses are complete.
- The previously shared test administrator password must be changed manually if still active; its value must never be reproduced.
- Provider credential rotation is not currently supported by evidence and will not be performed automatically.

## Implemented remediation awaiting final validation

- Added bootstrap-token-gated first-run setup, memory-hard bounded password verification, atomic source-based throttling, stricter session/cookie behavior, request-size limits, trusted-host validation, security headers, and query-safe access logging.
- Added Spotify PKCE/state validation and hardened pagination; replaced the legacy YouTube Music auth path with official Google OAuth endpoints, a narrower API scope, stable account identity checks, and complete disconnect cleanup.
- Added CSRF-protected review preparation, bounded provider work, persistent one-time approvals, provider-state binding, database pair leases, replay/stale-state rejection, failure-safe baselines, action journaling, and ambiguity-safe Spotify duplicate removal.
- Added Unicode-preserving identities and pair/provenance-scoped track mappings with migration `0009_security_remediation_state`.
- Enabled SQLite foreign-key enforcement and restrictive data/secret file permissions.
- Updated and hash-locked Python dependencies; removed `ytmusicapi`; upgraded `cryptography` to a patched release.
- Rebuilt the production container as a pinned multi-stage non-root image and added a loopback-by-default Compose profile with a read-only root filesystem, dropped capabilities, no-new-privileges, isolated secret storage, and resource limits.
- Added security regression tests across authentication, OAuth, middleware, provider adapters, database behavior, and synchronization integrity.

## Remaining work

- Manual operator actions listed in `SECURITY_REMEDIATION_REPORT.md`; these are intentionally outside this local task.
