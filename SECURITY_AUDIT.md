# Open Playlist Sync Security Audit

Audit date: 1 September 2026 (Australia/Sydney)  
Assessment type: authorized, audit-only, source/Git/runtime review  
Repository: `https://github.com/maus97/OpenPlaylistSync`  
Public revision assessed: `main` at `deafdc5e7ba8ab0b407fe590f61a13037581ca75`  
Live image assessed: `sha256:2ce61b92e4e326d709eec60ea32db09c21c7c2f07ec6f050be48fe88d797b975`

## 1. Executive Summary

**Verdict: NOT READY FOR PRODUCTION.**

Open Playlist Sync has a solid safety-oriented core, but the reproducible public release and the observed live network deployment are not yet suitable for production use with valuable accounts. The most important issue is release drift: public GitHub `main` has no application authentication, while the password protection in the live image exists only in uncommitted local files. Anyone who deploys the published branch with the published Compose file receives a privileged playlist-management interface on all host interfaces without a login barrier.

The live image is safer than public `main`: it requires a local administrator password, uses signed sessions and CSRF tokens, encrypts OAuth/client credentials at rest, runs as a non-root container user, and gates API documentation and sensitive UI routes. However, it is served directly over HTTP, the session cookie is not `Secure`, and TCP/8000 is published on IPv4 and IPv6. If the port is reachable from the local network, an on-path attacker could capture the administrator password or session and then use OPS to modify connected playlists.

The synchronization design includes unusually good controls: first sync is additive, conflicts block Apply, deletion needs explicit confirmation, state is re-read before a write, and YouTube removal uses the exact playlist-item ID. The remaining integrity weaknesses are still material. Apply is not serialized per pair, Spotify removal cannot bind approval to an exact duplicate occurrence, and canonical identity can collapse distinct non-ASCII tracks or be rewritten globally by another pair. These conditions can produce duplicate or unintended changes after an authenticated Apply. Destructive verification was intentionally not performed.

No reachable remote-code-execution, SQL-injection, command-injection, server-side template-injection, XSS, SSRF, path-traversal, unsafe-deserialization, Docker-socket, or CI secret-exfiltration path was found. Secret scanning found no committed or historical credentials. The image scanner's Critical/High package findings were investigated and do not reach OPS code paths, although the image still needs routine patching.

Finding counts:

| Critical | High | Medium | Low | Informational |
|---:|---:|---:|---:|---:|
| 0 | 1 | 7 | 6 | 0 |

Urgent action is required before a production deployment: make the authenticated build reproducible from GitHub, require HTTPS with no direct HTTP bypass, protect first-run bootstrap, and address the highest-risk synchronization integrity gaps.

## 2. Scope and Limitations

### Assessed

- Public GitHub repository contents and locally available Git history, including all visible branches/objects.
- Public `main` at commit `deafdc5` and the current uncommitted working tree as two distinct source states.
- Python/FastAPI routes, middleware, templates, OAuth clients, provider adapters, synchronization domain/coordinator/executor, scheduler, persistence models/repositories, migrations, configuration, and tests.
- Secrets in repository HEAD, history, working-directory paths, examples, documentation, CI, Docker definitions, and live Docker logs, with values redacted.
- Locked Python dependencies, direct-package currency, known Python advisories, and the actual live container image.
- GitHub Actions source and publicly visible run history.
- Declared Dockerfile/Compose configuration and actual container user, image, ports, networks, mounts, capabilities, seccomp state, health, limits, file modes, and volume metadata.
- The live local deployment at `http://127.0.0.1:8000`, using unauthenticated, low-volume HTTP requests only.
- Windows listeners for ports 80, 443, and 8000 and the readable portion of firewall profile state.

### Not assessed or only partially assessed

- No public hostname or external vantage point was available. Internet reachability, router/NAT policy, public DNS, and external TLS termination could not be tested.
- Windows denied access to enumerate the firewall rule associated with TCP/8000. Docker's all-interface bind is verified, but reachability from another physical LAN client was not.
- No reverse proxy was present in the observed deployment. There was no nginx, Caddy, Traefik, or Cloudflare configuration to inspect.
- GitHub branch protection, private Actions/repository/environment secrets, Actions policy, code-scanning settings, secret-scanning settings, and Dependabot security settings require authenticated repository-administrator access. Unauthenticated API calls returned 401.
- OPS intentionally has one local administrator rather than tenant users. Cross-tenant testing is not applicable; provider-side account ownership was assessed from code and provider documentation.
- No incorrect-password attempts, brute force, concurrency flood, load test, quota exhaustion, OAuth replay, account relink, destructive playlist operation, database write, container escape, or lateral movement was performed.
- Backup archives outside the repository/container, Docker Desktop VM internals, router/firewall appliances, and unrelated host services were unavailable.
- A previously supplied test password was not used or validated. It is intentionally not reproduced in this report.

These limitations did not prevent review of the source, published release, running image, local HTTP boundary, container isolation, or major playlist-integrity paths.

## 3. Architecture and Threat Model

OPS is a single-instance Python 3.12 application using FastAPI, Starlette sessions, Jinja2, SQLAlchemy, Alembic, SQLite, APScheduler, `httpx`, Spotify Web API, Google OAuth device authorization, and YouTube Data API v3. It has no separate frontend service and no multi-user tenant model.

```text
Browser / LAN client
        |
        | HTTP :8000 (current live deployment)
        v
FastAPI + signed client session + local-admin middleware
        |                         |
        |                         +--> Spotify OAuth / Web API
        |                         +--> Google OAuth / YouTube Data API
        v
SQLite database + Fernet key + session key in writable /data volume

GitHub source / pull request --> GitHub-hosted CI --> test + local image build only
```

Sensitive assets are Spotify and Google access/refresh tokens, OAuth client secrets, the Fernet credential key, the signed-session key, the administrator password verifier/session, playlist and account IDs, track mappings, accepted baselines, action journals, Docker host trust, backups, and GitHub workflow/repository authority.

Primary trust boundaries are:

- Unauthenticated browser to setup/login/health/static routes.
- Authenticated browser to every configuration, OAuth, pair, and sync route.
- OPS process to Spotify and Google APIs over TLS.
- Application process to `/data`; the same process can read both ciphertext and its decryption key.
- Container to Docker host and LAN. No host bind or Docker socket is mounted, but normal outbound networking is allowed.
- Pull-request/source input to GitHub-hosted CI and operator-built images.

Realistic threat actors include a client on a reachable LAN, an on-path local-network attacker, a malicious site targeting an authenticated browser, a local user or backup reader, a compromised dependency/action, a provider collaborator influencing playlist contents, and accidental/concurrent operator activity. The highest-impact outcomes are use of stored provider authority for unauthorized writes, token theft after volume/process compromise, destructive or duplicate synchronization, persistent lockout/quota exhaustion, and release/supply-chain compromise.

## 4. Findings Summary

| ID | Finding | Severity | Status | Affected component | Exploitability | Remediation priority |
|---|---|---|---|---|---|---|
| OPS-SEC-001 | Public `main` deploys the privileged UI without authentication | High | VERIFIED | GitHub `main`, FastAPI, Compose | Direct if published port is reachable | Immediate |
| OPS-SEC-002 | Password and session are transported over plain HTTP | Medium | VERIFIED | Live/default deployment | Adjacent/on-path; LAN reachability conditional | Immediate |
| OPS-SEC-003 | First-run administrator setup can be claimed by the first network visitor | Medium | HIGH-CONFIDENCE CODE/CONFIG FINDING | Authentication bootstrap | Fresh, reachable deployment only | Immediate |
| OPS-SEC-004 | Login throttling is race-prone and permits whole-account lockout DoS | Medium | HIGH-CONFIDENCE CODE/CONFIG FINDING | Local authentication/rate limit | Five requests for lockout; parallel race untested | Short term |
| OPS-SEC-005 | Sync review uses a state-changing, expensive GET without a CSRF/deduplication boundary | Medium | HIGH-CONFIDENCE CODE/CONFIG FINDING | Review/coordinator/provider APIs | Authenticated top-level navigation or public `main` | Short term |
| OPS-SEC-006 | Apply lacks pair-level serialization, one-time approval, and provider-version binding | Medium | HIGH-CONFIDENCE CODE/CONFIG FINDING | Sync coordinator/executor | Concurrent/replayed authenticated Apply | Immediate |
| OPS-SEC-007 | Spotify removal cannot bind approval to an exact duplicate occurrence | Medium | HIGH-CONFIDENCE CODE/CONFIG FINDING | Spotify adapter/sync domain | Duplicate URI or concurrent playlist edit | Immediate |
| OPS-SEC-008 | Canonical identities collide for Unicode and can be rewritten across pairs | Medium | VERIFIED | Sync domain/track mappings | Normal non-ASCII metadata or multi-pair remap | Short term |
| OPS-SEC-009 | OAuth authorization code and state are retained in access logs | Low | VERIFIED | Uvicorn/OAuth callback logs | Requires log access and usable code conditions | Short term |
| OPS-SEC-010 | YouTube account binding, scope, and disconnect lifecycle are weak | Low | HIGH-CONFIDENCE CODE/CONFIG FINDING | Google/YouTube OAuth/account storage | Operator account switching or later token compromise | Short term |
| OPS-SEC-011 | Browser response hardening and trusted-host policy are absent | Low | VERIFIED | FastAPI/reverse-proxy boundary | Primarily defense in depth | Medium term |
| OPS-SEC-012 | Storage permissions/key separation and SQLite integrity enforcement are incomplete | Low | VERIFIED | SQLite, `/data`, local Windows data | Requires host/volume/local access | Medium term |
| OPS-SEC-013 | Container hardening and image maintenance are incomplete | Low | VERIFIED | Docker image/runtime | Mostly post-compromise or resource abuse | Medium term |
| OPS-SEC-014 | CI and build inputs are not fully pinned, scanned, or currently green | Low | VERIFIED | GitHub Actions/build supply chain | Supply-chain compromise; CI impact currently limited | Medium term |

## 5. Detailed Findings

### Critical

No Critical finding survived validation. Scanner-labelled Critical packages were present in the image but their vulnerable features are not invoked by OPS.

### High

#### OPS-SEC-001 — Public `main` deploys the privileged UI without authentication

**Severity:** High  
**Status:** VERIFIED  
**Confidence:** High

**Affected component:** Public GitHub `main` at `deafdc5`; `src/ops/main.py:46-70`; privileged routes in `src/ops/api/routes.py`; `compose.yaml:5-6`.

**Description:** The public release does not install authentication or authorization middleware. It adds a signed-session middleware for CSRF state, but every settings, OAuth, provider-account, pair, review, baseline, and Apply route remains directly callable. The published Compose file maps TCP/8000 to every host interface.

**Technical evidence:**

- Remote `main` and local `origin/main` both resolved to `deafdc5e7ba8ab0b407fe590f61a13037581ca75` during the audit.
- `git show HEAD:src/ops/main.py` contains no `LocalAuthenticationMiddleware` and no login/setup gate.
- State-changing routes do require CSRF, but an unauthenticated direct client can obtain its own signed session and CSRF token from a normal page. CSRF prevents a third-party site from silently using another browser; it does not authenticate a direct client.
- `compose.yaml:5-6` publishes `8000:8000`, which Docker interprets as all IPv4/IPv6 host interfaces.
- The current live image is not evidence against this finding: its authentication files and migrations are uncommitted and absent from public `main`.

**Attack scenario:** An attacker who can reach a server cloned from public `main` opens `/pairs` or `/settings`, maintains the anonymous session, extracts the CSRF token, and invokes the same forms as an operator. The attacker can enumerate stored-account playlists through OPS, alter OAuth client configuration, disconnect accounts, create/delete mappings, review a plan, submit the published destructive confirmation phrase, and use stored OAuth authority to alter real playlists.

**Realistic impact:** Full application-level compromise and unauthorized provider-backed playlist actions. The UI does not directly reveal raw refresh tokens, but it acts as a confused deputy with those tokens.

**Existing mitigations:** CSRF tokens, provider-side ownership checks, additive-only initial sync, conflict detection, and explicit deletion confirmation reduce accidental/cross-site changes. None authenticates a direct attacker.

**Recommended remediation:** Land the authenticated implementation, migrations, templates, and tests in `main`; make authentication fail closed; make CI green and required before release; document a minimum secure version; and do not publish a container built from an unauthenticated revision.

**How to verify the remediation:** On a clean clone and empty volume, confirm that every route except a deliberately minimal health/static/bootstrap boundary redirects or returns 401/403 until authentication succeeds. Test `/settings`, `/pairs`, `/runs`, `/docs`, `/openapi.json`, OAuth start/callback routes, pair mutations, baseline, review, and Apply. Confirm the authenticated revision and image digest are reproducible from GitHub.

### Medium

#### OPS-SEC-002 — Password and session are transported over plain HTTP

**Severity:** Medium  
**Status:** VERIFIED  
**Confidence:** High

**Affected component:** `compose.yaml:5-17`; `src/ops/main.py:63-72`; actual container port/session configuration.

**Description:** The default and live deployment serve HTTP directly on all interfaces. Compose explicitly defaults `OPS_SESSION_COOKIE_SECURE=false`. The live cookie is `HttpOnly` and `SameSite=Lax`, but it lacks `Secure`; the administrator password and cookie therefore cross the network in cleartext.

**Technical evidence:**

- Runtime publishes `0.0.0.0:8000->8000/tcp` and `[::]:8000->8000/tcp`.
- `http://127.0.0.1:8000/auth/login` returned 200. An HTTPS handshake to port 8000 failed.
- Live `Set-Cookie` flags were `Path=/; Max-Age=28800; HttpOnly; SameSite=Lax`; `Secure` was absent.
- No process listened on host ports 80 or 443, and no reverse-proxy container was present.
- Windows firewall policy for port 8000 could not be enumerated, so reachability from another physical LAN host was not proven.

**Attack scenario:** A compromised LAN device, malicious access point, or other on-path actor intercepts an unlock request or authenticated request, captures the password or signed session cookie, and replays the session against OPS. The attacker can then perform any operation available to the administrator.

**Realistic impact:** Administrator credential/session theft and unauthorized playlist/configuration changes. HttpOnly does not protect a cookie in transit, and signing does not stop bearer-cookie replay.

**Existing mitigations:** The service is documented as private, the cookie is HttpOnly/SameSite, and authentication is enabled in the live image. A properly configured VPN or HTTPS proxy could mitigate the issue, but none was observed.

**Recommended remediation:** Put OPS behind an HTTPS-only reverse proxy or VPN; remove the direct all-interface bypass by using an internal-only network or a loopback-specific bind; set `OPS_SESSION_COOKIE_SECURE=true`; redirect or reject cleartext traffic; set HSTS after HTTPS is correct; and explicitly configure trusted proxy headers/client-IP handling.

**How to verify the remediation:** From a separate LAN client, direct TCP/8000 must be unreachable or restricted. HTTP must redirect to HTTPS without setting a session cookie. HTTPS responses must set `Secure; HttpOnly; SameSite=Lax/Strict`, and the advertised Spotify callback must use the exact HTTPS origin.

#### OPS-SEC-003 — First-run administrator setup can be claimed by the first network visitor

**Severity:** Medium  
**Status:** HIGH-CONFIDENCE CODE/CONFIG FINDING  
**Confidence:** High

**Affected component:** `src/ops/security/middleware.py:14-23`; `src/ops/api/routes.py:75-117`; default all-interface Compose publish.

**Description:** `/auth/setup` is intentionally public when administrator row 1 does not exist. There is no bootstrap token, console confirmation, source restriction, or local-only setup mode. A newly deployed instance is owned by whichever reachable client completes setup first.

**Technical evidence:** Middleware exempts `/auth/setup`; the route creates administrator ID 1 after only password confirmation and CSRF. CSRF is obtainable by the same direct client. The live database already has an administrator and `/auth/setup` redirects to login, so current re-claim is not possible without resetting/loss of the database.

**Attack scenario:** A fresh server starts on a reachable LAN before the owner visits it. Another client opens `/auth/setup`, chooses a password, and becomes the administrator.

**Realistic impact:** Complete application takeover at installation time, including later OAuth connection and playlist authority.

**Existing mitigations:** The window closes permanently after administrator creation; the route is CSRF-protected; current live setup is complete.

**Recommended remediation:** Require a high-entropy, one-time bootstrap token delivered outside HTTP, pre-provision the password through a local CLI/secret, or bind exclusively to loopback until setup completes. Make setup creation atomic and make deployment instructions require completing bootstrap before exposing any interface.

**How to verify the remediation:** Start with an empty volume. A remote client without the bootstrap secret must be unable to create the administrator. The intended local/bootstrap flow should work once and all later setup requests should fail closed.

#### OPS-SEC-004 — Login throttling is race-prone and permits whole-account lockout DoS

**Severity:** Medium  
**Status:** HIGH-CONFIDENCE CODE/CONFIG FINDING  
**Confidence:** High

**Affected component:** `src/ops/security/local_auth.py:13-18,97-125`; `src/ops/api/routes.py:146-168`.

**Description:** Five failed attempts from any source lock the only administrator account for 15 minutes. The nominal per-source threshold is ten, so the global lock always occurs first. Lock checks happen before the expensive scrypt computation and failure counters are read/updated/committed without an atomic increment or concurrency lock.

**Technical evidence:** `ACCOUNT_FAILURE_LIMIT=5`; `SOURCE_FAILURE_LIMIT=10`. The request reads `is_locked`/`source_is_limited`, performs scrypt, then calls `record_failure`. Parallel requests can all pass the pre-check and calculate roughly 32 MiB scrypt hashes before any lock is committed; stale SQLAlchemy objects can also lose increments. In this Docker Desktop runtime, host-forwarded requests appeared to OPS as the same bridge gateway (`172.18.0.1`), so source identity does not distinguish host-origin clients. No wrong-password or concurrency test was performed.

**Attack scenario:** A reachable client sends five ordinary incorrect attempts to deny access for 15 minutes. A parallel burst arrives before the first counter update, consuming significant memory/CPU and potentially obtaining more guesses than the intended threshold.

**Realistic impact:** Low-cost administrator denial of service and unreliable brute-force control. No direct password bypass was established.

**Existing mitigations:** Scrypt makes guesses expensive, errors do not reveal a username, source keys are hashed, lock state persists in SQLite, and successful login resets the account counter.

**Recommended remediation:** Replace the hard global lock with progressive delays and a rate limiter that cannot be weaponized against the sole account; atomically update counters; serialize or semaphore password verification; enforce body/password-size limits; use a trusted reverse-proxy client-IP source; add proxy/edge throttling; and set container resource limits. Calibrate password hashing at least to current OWASP guidance (`N=2^15,r=8,p=3` or a measured Argon2id configuration); OPS currently uses `p=1`.

**How to verify the remediation:** In isolated staging, use mocked/low-cost password verification to test concurrent requests without load. Confirm counters are atomic, only the intended number of verifications run, one source cannot lock out every other source, proxy client identities are correct, and oversized requests are rejected before hashing.

#### OPS-SEC-005 — Sync review uses a state-changing, expensive GET without a CSRF/deduplication boundary

**Severity:** Medium  
**Status:** HIGH-CONFIDENCE CODE/CONFIG FINDING  
**Confidence:** High

**Affected component:** `src/ops/api/routes.py:904-930`; `src/ops/sync/coordinator.py:239-313`; `src/ops/models.py:126-154`.

**Description:** `GET /sync/plan/{pair_id}` reads full provider playlists, creates and commits a `SyncRun`, searches the destination for unresolved additions, and commits provider-track mappings. It is therefore not a safe/read-only GET. There is no CSRF token, job deduplication, request rate limit, per-pair lease, or run-retention policy.

**Technical evidence:** `preview()` commits a run at `coordinator.py:244-262`; `unresolved_actions()` can issue one search for each distinct unmatched key and commits mappings at lines 303-312. A SameSite=Lax cookie is sent on a cross-site top-level GET. The live database already contained 27 runs, and no retention mechanism was found. The audit did not invoke review because it would consume live provider quota.

**Attack scenario:** A malicious site induces an authenticated operator to follow/open one or more review URLs, or an attacker with a stolen session repeatedly requests them. Large playlists consume worker time and provider quota while run/mapping records grow. On public unauthenticated `main`, no session compromise is needed.

**Realistic impact:** Provider quota exhaustion, degraded availability, database growth, and mapping changes without an explicit state-changing confirmation.

**Existing mitigations:** Current live authentication, provider timeouts for normal API calls, duplicate lookup caching within a review, cautious matching thresholds, and the fact that review never applies playlist writes.

**Recommended remediation:** Make review initiation a CSRF-protected POST; execute it as a bounded job with per-pair deduplication, concurrency limits, progress/cancellation, result caching and expiry; rate-limit it; cap playlist/work size; and prune old preview runs. Persist a mapping only as part of an explicit reviewed/accepted match or successful write.

**How to verify the remediation:** GET must not contact providers or change the database. Repeated POSTs for the same state should coalesce. Tests should assert CSRF enforcement, bounded work, run retention, mapping provenance, and provider-quota accounting.

#### OPS-SEC-006 — Apply is not serialized or bound to a one-time provider snapshot

**Severity:** Medium  
**Status:** HIGH-CONFIDENCE CODE/CONFIG FINDING  
**Confidence:** High

**Affected component:** `src/ops/api/routes.py:951-975`; `src/ops/sync/coordinator.py:382-446`; `src/ops/sync/safety.py:20-28`.

**Description:** The safety flow requires a reviewed plan and rechecks local state before Apply, but it has no per-pair execution lease, one-time approval token, provider-side version precondition, or idempotency key. The plan fingerprint omits provider item IDs, occurrences, order and provider snapshot/version data, and the same approved state can be submitted more than once.

**Technical evidence:** Apply loads the pair, recalculates and compares local state, then executes provider writes. No lock or unique active-job constraint surrounds that sequence. The fingerprint is derived from action type, canonical key and selected metadata; it does not bind approval to an exact provider snapshot. Spotify addition/removal calls do not provide idempotency tokens. No destructive or concurrent live test was performed. The live database had no accepted baselines, which reduces current deletion exposure but not duplicate-add exposure.

**Attack scenario:** Two browser submissions, workers, retries or scheduled/manual races pass the same precondition before either commits. Both issue the same add/remove calls, or the remote playlist changes between review and Apply while the local fingerprint still matches.

**Realistic impact:** Duplicate tracks, conflicting remote writes, partial playlist corruption, or an operation differing from what the operator reviewed. Exploitation requires an authenticated/replayed operation or an operational race; this is not a remote code-execution issue.

**Existing mitigations:** Explicit review, exact deletion confirmation, a local state fingerprint, conflict blocking, first-sync additive behavior, execution journaling, and post-execution baseline updates.

**Recommended remediation:** Add a database-backed per-pair lease/unique active job; issue a one-time, expiring approval nonce bound to the full ordered plan, exact provider item identifiers and provider versions; consume it atomically; use provider preconditions where available; make retry decisions from the execution journal; and disable double submission in the UI.

**How to verify the remediation:** In staging with disposable playlists, race two identical Apply requests and simulate a retry after a timeout. Exactly one mutation set should occur. Change a provider playlist between Review and Apply and confirm Apply stops for re-review.

#### OPS-SEC-007 — Spotify deletion cannot guarantee the reviewed duplicate occurrence

**Severity:** Medium  
**Status:** HIGH-CONFIDENCE CODE/CONFIG FINDING  
**Confidence:** High

**Affected component:** `src/ops/providers/spotify.py:121-152,336-353`; synchronization action identity and deletion execution.

**Description:** OPS models duplicate occurrences, but the Spotify delete request identifies only a track URI. It does not carry the playlist snapshot obtained during review or another exact occurrence identifier. Consequently, the operation cannot prove that the provider removed precisely the occurrence reviewed by the user when duplicate URIs or concurrent playlist edits exist.

**Technical evidence:** Spotify playlist reads do not preserve a snapshot/version in the plan, and `remove_track()` sends an item containing only the URI. Spotify's current endpoint accepts item URIs and an optional snapshot identifier, but no position-specific occurrence selector. The audit intentionally did not perform a deletion test. The issue is therefore a high-confidence integrity finding rather than a claim that Spotify will always remove all duplicate copies. See Spotify's [Remove Playlist Items reference](https://developer.spotify.com/documentation/web-api/reference/remove-items-playlist) and [playlist snapshot documentation](https://developer.spotify.com/documentation/web-api/concepts/playlists).

**Attack scenario:** A playlist contains the same Spotify track more than once, or someone edits it after Review. Apply sends a URI-only removal and the resulting occurrence/count is not the exact state the user approved.

**Realistic impact:** The wrong duplicate occurrence or an unintended number/state of occurrences may remain or be removed, undermining occurrence-aware synchronization. The impact is playlist integrity, not account compromise.

**Existing mitigations:** Review/confirmation, local occurrence-aware reconciliation, first-sync safety, and a post-operation re-read before accepting the new baseline.

**Recommended remediation:** Until exact behavior is proven safe for every duplicate case, block or require heightened confirmation for automatic Spotify deletion involving duplicate URIs. Bind plans to Spotify snapshot IDs and abort on drift. Evaluate a desired-list replacement strategy only if it preserves ordering, local items, unavailable tracks and concurrent edits safely.

**How to verify the remediation:** Use a disposable staging playlist with several identical URIs at known positions. Exercise single deletion, concurrent reorder and stale-snapshot cases; require exact expected counts/order and fail-closed behavior before enabling automatic deletion.

#### OPS-SEC-008 — Canonical identity and global mapping collisions can corrupt reconciliation

**Severity:** Medium  
**Status:** VERIFIED  
**Confidence:** High

**Affected component:** `src/ops/domain.py:13-28`; `src/ops/models.py:96-107`; `src/ops/sync/coordinator.py:179-223,265-312`.

**Description:** Text identity normalizes Unicode with NFKD and discards every non-ASCII character. Distinct tracks written entirely in non-Latin scripts can therefore collapse to the same empty or partial canonical key. Separately, a provider-track mapping is unique only by provider account and provider-track ID, not by pair or mapping provenance; saving a later match overwrites the canonical key globally and affects other playlist pairs.

**Technical evidence:** A safe local unit probe using two different Chinese title/artist pairs produced the same key, `text:|`. The mapping schema's uniqueness and `save_track_mapping()` upsert behavior allow a Review on one pair to reclassify the same provider item for every other pair on that account. Review itself commits inferred mappings. The live database contained zero mapping rows, so no current collision was observed.

**Attack scenario:** Legitimate or deliberately crafted provider metadata collides after ASCII stripping, or a weak inferred match in one pair overwrites a mapping used by another. A later plan treats distinct items as equal or changed and schedules the wrong addition/removal.

**Realistic impact:** Silent mismatches, missing songs, duplicate songs or destructive propagation after a baseline has been accepted. This is especially relevant for non-Latin catalogues and multiple pairs sharing an account.

**Existing mitigations:** Stable provider IDs are preferred when available; matching has score thresholds and version-marker logic; unresolved tracks are shown to the user; no live mappings currently existed.

**Recommended remediation:** Preserve Unicode using a deliberate NFKC/casefold/punctuation policy; never reduce arbitrary non-Latin identity to an empty key; include duration/ISRC/provider evidence where available; make mappings pair-scoped or store auditable provenance/confidence; prevent silent cross-pair overwrite; and require re-review/re-baselining after the migration.

**How to verify the remediation:** Add property-based and corpus tests covering multiple scripts, combining marks, emoji, punctuation and version labels. Test two pairs that map the same provider item differently and confirm neither can silently mutate the other's reconciliation result.

### Low

#### OPS-SEC-009 — OAuth authorization code and state are retained in access logs

**Severity:** Low  
**Status:** VERIFIED  
**Confidence:** High

**Affected component:** Uvicorn access logging; Spotify callback at `src/ops/api/routes.py:463-470`.

**Description:** The live container access log contains a Spotify callback request whose query string includes `code` and `state`. The audit counted and classified the parameters without reproducing their values. No access token, refresh token or client secret was found in the current container log.

**Technical evidence:** One callback URL and one occurrence each of the `code` and `state` parameter names were found. The callback consumes the state before redirecting. OAuth authorization codes are short-lived and normally single-use, so a code in a log after successful exchange is substantially less sensitive than a refresh token, but it remains credential material during the exchange window.

**Attack scenario:** A log reader obtains a still-valid authorization code and its state and attempts to race the legitimate callback, or retains authentication metadata longer than necessary.

**Realistic impact:** A narrow opportunity for OAuth flow interference plus avoidable leakage into backups/support bundles. No reusable live token exposure was demonstrated.

**Existing mitigations:** Strong random state, state is popped on callback, Spotify codes are short-lived/single-use, tokens are encrypted in the database, and no token response is logged.

**Recommended remediation:** Configure access-log query redaction or suppress callback query strings, ensure exception/logging paths never serialize OAuth responses, minimize log retention, and restrict log access.

**How to verify the remediation:** Complete a disposable OAuth reconnect and confirm logs contain only the callback path/status with all credential-like query values absent or irreversibly redacted.

#### OPS-SEC-010 — YouTube connection identity, scope and disconnect lifecycle are over-broad

**Severity:** Low  
**Status:** HIGH-CONFIDENCE CODE/CONFIG FINDING  
**Confidence:** High

**Affected component:** `src/ops/api/routes.py:528-590` and account-disconnect handling; YouTube OAuth configuration.

**Description:** The YouTube device flow requests the broad `youtube` scope, stores the connected account under the hard-coded external identity `default`, and does not fetch/verify the authenticated channel identity. Reconnecting a different Google account can overwrite that logical account while existing pairs retain provider playlist identifiers. Disconnect removes the selected local credential but does not revoke the grant; historically replaced credential rows/tokens can remain unless explicitly cleaned.

**Technical evidence:** Successful connection creates/updates `external_account_id="default"`. No `channels.list(mine=true)` identity binding was found. The requested scope is `https://www.googleapis.com/auth/youtube`; Google's narrower `youtube.force-ssl` scope authorizes the playlist create/insert/delete operations OPS uses. See Google's [OAuth scope catalogue](https://developers.google.com/identity/protocols/oauth2/scopes) and [YouTube server-side OAuth guide](https://developers.google.com/youtube/v3/guides/auth/server-side-web-apps).

**Attack scenario:** An administrator reconnects the wrong Google identity. Existing pairs silently target IDs under a different credential and fail or operate on unexpected playlists that happen to be accessible. A disconnected authorization remains valid at Google until revoked/expired.

**Realistic impact:** Account confusion, stale credential exposure and an unnecessarily broad blast radius if a YouTube token is stolen. Provider authorization still prevents access to playlists the credential cannot control.

**Existing mitigations:** Single-administrator UI, encrypted token storage, CSRF protection, device authorization at Google, and provider-side access control. Device authorization does not use a browser redirect state/PKCE exchange, so absence of those mechanisms there is not itself a flaw.

**Recommended remediation:** Fetch and display a stable channel/account identifier after connection; bind pairs to it and pause/revalidate them on account change; request the least scope that supports required operations (currently `youtube.force-ssl` appears sufficient); clearly distinguish local disconnect from provider revocation; and provide safe cleanup/revocation guidance for replaced credentials.

**How to verify the remediation:** Connect two disposable Google identities in sequence. Confirm the identity change is explicit, prior pairs are disabled pending review, the narrow scope supports all playlist operations, and disconnect/revoke behavior matches the UI wording.

#### OPS-SEC-011 — Browser and host-boundary response hardening is absent

**Severity:** Low  
**Status:** VERIFIED  
**Confidence:** High

**Affected component:** FastAPI middleware and live HTTP responses.

**Description:** Live responses do not set Content-Security-Policy/frame-ancestors, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, HSTS or explicit no-store controls for authenticated/sensitive pages. Host headers are not allowlisted.

**Technical evidence:** Direct header inspection confirmed the controls were absent. A request with `Host: attacker.invalid` was accepted and returned the login page, although the value was not reflected and did not influence a redirect. CORS was not enabled. Template review found autoescaping and no unsafe `safe`/`Markup`/DOM injection sinks, so no XSS or practical Host-header exploit was proven.

**Attack scenario:** A future rendering defect has greater impact without CSP; the UI can be framed for clickjacking; referrer/cache behavior may disclose sensitive navigation; or a future absolute-URL feature trusts an attacker-controlled Host header.

**Realistic impact:** Defense-in-depth weakness rather than a demonstrated standalone compromise. Severity could increase if a new injection or host-derived URL path is introduced.

**Existing mitigations:** Jinja autoescaping, CSRF tokens on state changes, SameSite cookies, no broad CORS policy and no discovered script injection sink.

**Recommended remediation:** Add a restrictive CSP using nonces/hashes for the existing inline script, `frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, a conservative Referrer-Policy and Permissions-Policy, no-store caching on login/settings/review pages, and TrustedHost enforcement. Add HSTS only at the HTTPS edge after TLS is consistently enforced.

**How to verify the remediation:** Run integration tests over representative HTML, redirects, static files and errors; confirm headers are present without breaking HTMX/forms and unapproved Host values are rejected.

#### OPS-SEC-012 — Database integrity enforcement and at-rest key separation are weak

**Severity:** Low  
**Status:** VERIFIED  
**Confidence:** High

**Affected component:** `src/ops/db.py:19-22`; `/data` Docker volume; local repository `data` directory and backup model.

**Description:** SQLite foreign-key enforcement is not enabled, and the live database is mode 0644 inside a mode-0755 data directory. The Fernet encryption key and encrypted database reside in the same mounted volume. The local development data directory also inherited broad Windows user ACLs. Encryption therefore protects secrets from a database-only disclosure, but not from volume/host compromise or a backup that contains both files.

**Technical evidence:** A read-only live query returned `PRAGMA foreign_keys=0`. `/data/ops.db` was 0644; encryption/session keys were 0600. Provider credentials and configured client secrets were Fernet-looking ciphertext, not plaintext. The local Windows ACL allowed ordinary local users to read/modify the data directory; its current database had zero administrators, accounts, configuration, pairs, baselines and runs, so it did not expose active tokens. No key-rotation facility was found.

**Attack scenario:** A host user, container compromise, stolen unencrypted backup, or overly broad volume copy obtains both database and key and decrypts all stored provider credentials. Disabled foreign keys allow application bugs/concurrency to leave orphaned integrity state.

**Realistic impact:** Stored OAuth/client-secret compromise after host/volume access and increased risk of inconsistent sync records. This is not an externally reachable database: SQLite is a file in a named volume and has no listening service.

**Existing mitigations:** Fernet encryption, random key generation, non-root container execution, key files mode 0600, no database port, `.gitignore`/`.dockerignore` exclusions and no historical secret found.

**Recommended remediation:** Enable SQLite foreign keys on every connection and add constraint tests; make the data directory 0700 and database 0600; tighten Windows ACLs; keep the encryption key in a separate secret facility/mount with a recovery and rotation design; and document encrypted, access-controlled backups that never casually bundle key and data.

**How to verify the remediation:** Confirm every pooled connection reports `foreign_keys=1`; invalid foreign-key writes must fail in tests. Verify runtime/host ACLs with an unprivileged identity, exercise key rotation in staging, and restore a protected backup without exposing secrets.

#### OPS-SEC-013 — Container is well de-privileged but lacks containment and patch hardening

**Severity:** Low  
**Status:** VERIFIED  
**Confidence:** High

**Affected component:** `Dockerfile`, `compose.yaml`, live image `open-playlist-sync:local` and container `openplaylistsync-ops-1`.

**Description:** The live container correctly runs as a non-root user with no effective/permitted capabilities, default seccomp, no Docker socket, no host mounts and no devices. However, its root filesystem is writable, `/app/templates` is owned by the application user, `no-new-privileges` is off, a default capability bounding set and setuid utilities remain, egress is unrestricted, and Compose sets no CPU/memory/PID limits or explicit capability drop.

**Technical evidence:** Runtime inspection showed UID 100/GID 101, effective/permitted capabilities zero, seccomp mode 2, `Privileged=false`, `ReadonlyRootfs=false`, `NoNewPrivileges=0`, one bridge network and one RW named volume at `/data`. Only TCP/8000 was published; no Docker socket/host path was mounted. Docker Scout reported raw image counts of 2 Critical, 6 High, 10 Medium and 31 Low vulnerabilities. Manual reachability review found all Critical/High items in unused features: Perl archive/glob/Socket/regex paths, OpenSSL CMS/CMP/DTLS, and `cryptography` PKCS#7 decryption; OPS uses none of them. Therefore no reachable application Critical/High image vulnerability was retained. The actual image digest inspected was `sha256:2ce61...REDACTED`.

**Attack scenario:** If a future application vulnerability yields code execution, writable application files, remaining system tooling, broad egress and missing resource limits make persistence within the container, credential exfiltration and denial of service easier. No host escape path was demonstrated.

**Realistic impact:** Increased post-compromise blast radius and maintenance risk. Direct host compromise is materially constrained by non-root execution, zero effective capabilities and lack of host/Docker mounts.

**Existing mitigations:** Non-root `USER`, minimal slim base, health check, named data volume, default seccomp, no privileged mode/socket/devices/host network and a single-container network.

**Recommended remediation:** Rebuild routinely on a patched minimal base; upgrade `cryptography`; use a multi-stage/runtime-minimal image; remove unused Perl/build/package tools and setuid binaries; set `cap_drop: [ALL]`, `no-new-privileges:true`, read-only root filesystem plus explicit tmpfs/data writes, and conservative PID/memory/CPU limits. Restrict outbound network access if operationally feasible.

**How to verify the remediation:** Re-scan the exact resulting digest, run the full integration suite, assert non-root/zero caps/no-new-privileges/read-only root at runtime, and confirm OPS can write only its intended data paths and reach only necessary provider/DNS destinations.

#### OPS-SEC-014 — CI and build inputs are mutable and security governance is incomplete

**Severity:** Low  
**Status:** VERIFIED  
**Confidence:** High

**Affected component:** `.github/workflows/ci.yml`; `Dockerfile`; `requirements.lock`; public GitHub repository settings visible without administrator access.

**Description:** The workflow has a sound low-privilege design but relies on mutable major action tags, installs the latest `uv`, uses a mutable base-image tag, and installs a requirements lock without package hashes. Build-only requirements are range/unpinned. No Dependabot/Renovate configuration, security policy or repository security scanner workflow exists. The newest two public `main` runs failed in tests and skipped the container job.

**Technical evidence:** Workflow permissions are explicitly `contents: read`; triggers use `pull_request`, not `pull_request_target`; jobs are GitHub-hosted and do not consume repository secrets, publish images or deploy. It references `actions/checkout@v4` and `actions/setup-python@v5`, then obtains unpinned `uv`. The Docker base is `python:3.12-slim`, and `setuptools>=77`/`wheel` are not exact. Public APIs could not reveal branch protection, secret scanning, private secrets/environments or Actions policy without administrator authorization.

**Attack scenario:** A compromised upstream action/tool/tag or mutable base image changes what CI/builds execute. A malicious pull request cannot currently read production secrets or publish an image through this workflow, which materially limits direct exploitability.

**Realistic impact:** Supply-chain compromise or unreproducible builds, with low current CI/CD blast radius because no release/deploy authority is present. Failed default-branch CI reduces confidence that the published revision is releasable.

**Existing mitigations:** Minimal explicit token permissions, safe PR trigger, no secrets/deployment/publishing, exact runtime dependency versions, public code review and a container build gate after tests.

**Recommended remediation:** Pin third-party Actions to commit SHAs, pin `uv` and base image digests with a controlled update process, generate hash-checked dependency locks including build dependencies, add Dependabot/Renovate and secret/static/dependency/image scans, publish SBOM/provenance for releases, require passing protected-branch checks and add `SECURITY.md`.

**How to verify the remediation:** Inspect the workflow for immutable references and least privileges; attempt an untrusted fork PR and confirm it receives no secrets/write token; reproduce the image digest from documented inputs; and require a green default-branch pipeline before release.

### Informational

No separate informational finding was assigned. Correctly implemented defenses and non-vulnerabilities are recorded in Section 13 so that scanner noise is not inflated into findings.

## 6. OAuth / Authentication Security

### Local administrator authentication

The current working tree and live image implement one local administrator. Passwords are stored as salted scrypt verifiers and checked with constant-time comparison; setup/change enforce 12–256 characters. Login clears the old session, a password change increments a generation value that invalidates prior sessions, logout clears the session, and authenticated pages are gated by middleware. Sessions expire after eight hours and the cookie is `HttpOnly` and `SameSite=Lax`.

The public `main` revision has none of that authentication and is therefore the primary release risk (OPS-SEC-001). In the live build, the missing `Secure` cookie/HTTPS boundary is the primary session risk (OPS-SEC-002). The first-run claim and rate-limit design are covered by OPS-SEC-003/004. Current scrypt parameters (`N=2^15,r=8,p=1`) are meaningful protection but fall below current [OWASP Password Storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html), which lists `p=3` at the same N/r setting. This is a calibration/hardening issue, not evidence that the tested password verifier can be cracked.

No session fixation, signature bypass, unauthenticated route bypass in the live build, or password disclosure in application files/logs was found. A test password was supplied in the project conversation before this audit. It was not used, tested, stored in either report, or reproduced. If it remains configured, it should be changed during remediation because disclosure outside the application defeats even a strong verifier.

### Spotify

- **Flow and state:** OPS uses the confidential-client Authorization Code flow. It generates a high-entropy state value, stores it in the signed session, checks it with constant-time comparison, and removes it before code exchange. The callback accepts only a code matching the pending flow. See Spotify's [Authorization overview](https://developer.spotify.com/documentation/web-api/concepts/authorization).
- **PKCE:** PKCE is not implemented. For this server-side confidential flow, random one-time state plus a protected client secret prevents the common public-client interception case, so this was not reported as a standalone vulnerability. PKCE remains useful defense in depth; Spotify documents it separately in its [PKCE flow](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow).
- **Redirects:** The callback URI is configured server-side and used as an exact provider redirect. No user-controlled open redirect was found. The current default is HTTP and inherits OPS-SEC-002.
- **Scopes:** `user-read-private`, private/collaborative playlist read, and private/public playlist modification correspond to the application's enumeration and synchronization functions. No unrelated account/email/library scope was requested.
- **Tokens:** Access/refresh tokens are returned to backend code, encrypted with Fernet before database storage, refreshed server-side, and not rendered to the frontend. Refresh logic preserves the existing refresh token when Spotify omits a replacement. The callback query is logged (OPS-SEC-009), but no access/refresh token was found in the live log.
- **Isolation:** The UI is a single-admin application rather than a tenant service. Provider account rows are selected through authenticated flows and Spotify enforces playlist authority. No cross-user token route was found; public `main` nevertheless lets any reachable caller act through every stored connection.

### Google / YouTube Music

- **Flow:** Playlist writes use the official YouTube Data API v3. Device authorization is performed by `ytmusicapi`; the device code is temporary and held in the signed browser session while tokens remain backend-side. Device flow does not use a browser callback state/PKCE exchange, so applying web-flow requirements literally would be a false positive.
- **Scope:** OPS requests the broad `youtube` scope. The narrower `youtube.force-ssl` scope appears to cover `playlists.insert`, `playlistItems.insert`, and `playlistItems.delete`; scope reduction should be integration-tested. Relevant provider references are [playlists.insert](https://developers.google.com/youtube/v3/docs/playlists/insert), [playlistItems.insert](https://developers.google.com/youtube/v3/docs/playlistItems/insert), and [playlistItems.delete](https://developers.google.com/youtube/v3/docs/playlistItems/delete).
- **Identity and lifecycle:** OPS does not resolve the authenticated channel and uses `default` as its external account identity. Reconnection/account switching and provider revocation semantics are insufficiently explicit (OPS-SEC-010).
- **Tokens:** Tokens are encrypted at rest, kept out of rendered pages and refreshed server-side. Disconnect removes the local selected credential but is not equivalent to provider revocation. Database-plus-key compromise would expose every stored token (OPS-SEC-012).
- **Playlist item identity:** YouTube reads retain playlist-item IDs and deletion uses that exact ID, which is stronger than the Spotify duplicate-removal path.

### Session and user-isolation conclusion

The live app's session signature and CSRF implementation are appropriate for a single local administrator, provided transport security is added. There is no multi-tenant authorization layer because there are no tenant users; all authenticated actions intentionally share the administrator's connected accounts. That makes password/session compromise equivalent to full OPS authority. Public `main` has no meaningful user isolation at all.

## 7. Synchronization Integrity

| Security question | Assessment |
|---|---|
| Are unauthorized playlist changes possible? | **Yes on public `main`** because it has no authentication. In the live build, no auth bypass was found, but a stolen HTTP session/password or replayed/raced Apply can exercise stored authority. |
| Is first sync safe? | **Generally yes.** With no accepted baseline, reconciliation is additive and does not propagate deletions. Baseline acceptance is explicit. |
| Is destructive deletion propagation possible? | **Yes, intentionally after baseline and confirmation.** Conflicts block and deletion needs an exact confirmation phrase, but Spotify duplicate semantics and stale/concurrent provider state can make the result differ from review. |
| Are retries idempotent? | **Only partially.** Local state is recalculated and each action is journaled, but provider writes have no operation idempotency key and Apply approvals can be reused/raced. |
| Are duplicates handled correctly? | **Partially.** Reconciliation models occurrences and YouTube deletes exact playlist-item IDs. Spotify URI-only removal cannot guarantee an exact reviewed occurrence. |
| Is partial failure safe? | **Recoverable, not transactional.** Successful/failed/skipped actions are journaled and the baseline advances only from a post-operation read, but remote writes cannot be rolled back atomically; a subsequent review must reconcile them. |
| Is concurrent synchronization safe? | **No.** Scheduler-level `max_instances=1` protects one scheduler job, but there is no database-backed per-pair exclusion covering manual, scheduled, multi-worker or replayed Applies. |
| Are synchronization loops prevented? | **Mostly, but not proven under every edge case.** Three-way baselines and post-apply reads suppress normal ping-pong. Global mapping overwrite, Unicode collisions, skipped/unresolved acceptance and concurrent changes can create recurring or incorrect plans. |

Additional integrity observations:

- Review marks conflicts and blocks Apply; it does not silently resolve simultaneous changes.
- Apply re-reads provider state and compares a local fingerprint before writes, an important control that nevertheless lacks exact provider snapshot binding.
- Deletion requires an explicit phrase and the displayed plan; no hidden automatic deletion path was found.
- Scheduled sync generates previews and does not automatically Apply destructive plans.
- YouTube removal uses playlist-item IDs; Spotify additions/removals are provider URI based.
- Search/matching uses thresholds and version markers, including acoustic/live/remix distinctions, but its canonical-key foundation is unsafe for non-ASCII data.
- An unresolved/skipped action can become part of the accepted post-run baseline. This avoids repeated provider writes but can normalize divergence; the UI should make that consequence explicit.
- The live database had two pairs, 27 runs, 190 action rows, zero accepted baselines and zero provider-track mappings. At audit time, therefore, deletion propagation was not active for the observed pairs, but the code-path risks remain.

No real playlist write or deletion was performed. OPS-SEC-006/007 are high-confidence findings for which destructive verification was intentionally not performed.

## 8. Secrets Review

No current or historical repository secret was discovered.

- Gitleaks scanned the complete reachable Git history and working directory with redaction enabled and reported no leak.
- Git object/path review found only placeholder/example secret names, `.gitkeep`, and expected documentation—not credentials.
- `.gitignore` excludes environment files, databases, data directories, key material and common private-key formats. `.dockerignore` excludes Git metadata, GitHub metadata, development data, environment files, tests and documentation from the image build context.
- Current configuration/client-secret and provider-token columns in the live database were ciphertext consistent with Fernet, not plaintext. Values were not extracted or decrypted.
- The Fernet key, session key and SQLite database exist in the expected Docker volume; none is committed. Key/data co-location is covered by OPS-SEC-012.
- Live environment-variable **names** were inspected without printing values. Sensitive generated keys/provider secrets were not passed as raw container environment values.
- Live logs contained one Spotify callback with authorization-code and state query parameters (OPS-SEC-009), but no `access_token`, `refresh_token` or `client_secret` values and no traceback/500 response in the current container log.
- The local development data directory contains ignored key/session files, but its local database had no active administrator, provider account, configuration, pair, baseline or run data. Its ACL is still broader than appropriate.

No Spotify/Google OAuth credential or application client secret requires immediate rotation on the evidence available. If the previously shared test administrator password remains configured, change it as the first remediation action; that recommendation is based on its disclosure in conversation, not on a repository/runtime leak. OAuth authorization grants should be rotated/revoked only if later log/backup review establishes exposure, because this audit found none.

## 9. Dependency / Supply-Chain Review

The Python runtime is locked to exact application package versions, and direct dependencies were generally current and not yanked. Meaningful results were:

| Source | Result | Validation and disposition |
|---|---|---|
| `pip-audit` against `requirements.lock` | `cryptography 49.0.0` affected by CVE-2026-69247 / PYSEC-2026-3552; fixed in 50.0.0 | The flaw is in PKCS#7 decryption. OPS uses Fernet and does not call PKCS#7 decrypt functions, so it is not a reachable High in OPS. Upgrade remains recommended. See [OSV PYSEC-2026-3552](https://osv.dev/vulnerability/PYSEC-2026-3552). |
| Docker Scout on the exact live image | 2 Critical, 6 High, 10 Medium, 31 Low, plus unknown-severity advisories | Every Critical/High was manually checked. They affect unused Perl archive/glob/Socket/regex paths, OpenSSL CMS/CMP/DTLS, or the same cryptography PKCS#7 path. None is invoked or exposed by OPS; scanner severity was not copied into application severity. |
| Direct-package currency check | `authlib`, `cryptography`, and `uvicorn` had newer releases; `ytmusicapi` was current | Normal update backlog, not proof of exploitability. The project currently caps `cryptography<50`, preventing the advisory fix until compatibility is validated. |
| Bandit | 0 High, 1 Medium, 2 Low | The Medium is expected `0.0.0.0` binding and is represented by deployment findings. Low results were false positives/non-security assertions. |
| Semgrep OWASP/Python rules | 0 findings across 95 files / 154 rules | Supporting evidence only; manual flow review was still performed. |

Supply-chain weaknesses are recorded in OPS-SEC-014: hashes/digests and build tooling are not fully pinned, and automated maintenance/security policy is incomplete. No questionable package index, Git dependency, arbitrary URL dependency, npm ecosystem, vendored binary, or runtime package installation was found. The image contains more OS tooling than OPS needs, increasing advisory churn and potential post-compromise utility.

## 10. Docker / Runtime Review

The actual container matched the current dirty working-tree code for the critical entrypoint, routes, local authentication, coordinator and provider adapters by SHA-256 comparison. It did **not** match public `main`, which lacks authentication. This uncommitted release drift is the most important declared-versus-runtime difference.

| Property | Declared configuration | Observed runtime | Security assessment |
|---|---|---|---|
| Image | Locally built Python 3.12 slim image | `open-playlist-sync:local`; Python 3.12.14; exact digest recorded above | Reproducibility/provenance absent; scanned actual digest |
| User | `USER ops` | UID 100, GID 101 | Positive: non-root |
| Privilege/capabilities | No privileged/socket/device declaration | `Privileged=false`; effective/permitted caps zero; default bounding set remains | Strong base, add explicit drop/no-new-privileges |
| Seccomp | Default | Seccomp mode 2 | Positive |
| Root filesystem | Not read-only | Writable; application user owns writable application/template paths | Hardening gap |
| Storage | Named volume at `/data` | One RW named volume; no bind mount; no Docker socket | Host pivot constrained; DB/key share volume |
| Network | Port `8000:8000`; bridge | IPv4 `0.0.0.0:8000` and IPv6 `[::]:8000`; one non-internal bridge with only OPS | Direct LAN boundary possible; unrestricted egress |
| Health/restart | Health check and restart policy configured | Healthy and running | Positive availability control |
| Resource isolation | No limits | No CPU, memory or PID limit | DoS blast-radius gap |
| Files | App copied then privilege changed | `/data` 0755, database 0644, key files 0600 | Tighten directory/database modes |

Only one running Docker container was observed, and no other Docker service/database/management UI was connected to its network. There was no host networking, host PID/IPC namespace, device exposure, Docker socket, host bind mount, or added capability. A direct container escape or lateral path was not attempted and no plausible one was identified from configuration.

Container compromise would expose the application's in-memory/decryptable OAuth credentials and RW database volume and could make outbound connections. It would not automatically provide Docker control or direct host filesystem access. The residual host risk is therefore lower than the token/playlist risk, although kernel/runtime vulnerabilities and unrestricted egress remain outside application containment. OPS-SEC-013 contains the image and hardening recommendations.

## 11. GitHub / CI/CD Review

The public repository and all locally reachable Git objects were assessed. `main` and `origin/main` resolved to commit `deafdc5e7ba8ab0b407fe590f61a13037581ca75`. The live security changes are not in that commit, producing the High release-drift finding.

The single workflow is comparatively safe against malicious pull requests:

- It uses `pull_request` rather than `pull_request_target`.
- Top-level `GITHUB_TOKEN` permission is explicitly `contents: read`.
- Jobs run on GitHub-hosted runners.
- No repository/environment secret is referenced.
- It does not publish an image, create a release, deploy, push a commit or access a production host.
- A malicious fork PR could run untrusted tests within its isolated job, but on the visible configuration it cannot obtain a privileged token or deployment secret. GitHub-hosted runner compromise would be discarded with the job.

The remaining issues are supply-chain reproducibility and governance (OPS-SEC-014): mutable action tags/tools/base image, no hash-checked lock, no dependency-update/security workflow and no security policy. The newest two public `main` workflow runs failed at tests and skipped the container job; an open fix workflow required action at audit time. A release should not be treated as validated until the default branch is green.

Public, unauthenticated GitHub endpoints do not disclose branch rules, private secrets/environments, Actions policy, secret scanning or code-scanning settings for this repository. Administrator access would be required to verify protected branches, required reviews/checks, force-push/deletion restrictions, two-person release control, secret scanning/push protection and allowed-actions policy. No evidence suggests that a PR currently can modify production or publish a compromised official image because no such workflow exists.

## 12. Live Deployment Review

The live deployment was examined as an unauthenticated external client using a small number of ordinary requests. The existing signed-in browser session was not used, no provider-backed Review was invoked, and no login failure or data mutation was attempted.

### Network and TLS

- Docker publishes TCP/8000 on all IPv4 and IPv6 host interfaces. Host listener inspection found the Docker/WSL forwarding processes on 8000 and no listener on 80 or 443.
- `http://127.0.0.1:8000` works; a TLS handshake to port 8000 fails. There is no observed HTTP-to-HTTPS redirect, reverse-proxy container or local TLS terminator.
- Windows firewall profiles were enabled, but access to enumerate the matching port rule was denied. Thus all-interface exposure is proven while reachability from a second physical LAN host is not.
- No public hostname, router exposure or external vantage point was supplied, so Internet exposure cannot be asserted.

### HTTP, authentication and exposed endpoints

| Check | Observed result |
|---|---|
| `/healthz` | 200 with minimal health JSON; intentionally public |
| `/auth/login` | 200; establishes signed session/CSRF cookie |
| `/auth/setup` | 303 to login because the live administrator already exists |
| `/`, `/pairs`, `/settings` | 303 to login when unauthenticated |
| `/docs`, `/openapi.json` | 303 to login in the live build |
| `/.env`, `/.git/config`, `/data/ops.db`, `/backup.zip` | 303 to login, not file disclosure |
| `/static/../.env` | 404; no traversal found |
| Invalid session cookie | Rejected by redirect to login |
| TRACE | 405 |
| Cross-origin request | No `Access-Control-Allow-Origin`; CORS is not broadly enabled |
| Untrusted Host value | Login page accepted; no reflection or redirect effect observed |
| Server errors | No 500/traceback in current container log; malformed low-impact paths did not expose debug data |

The cookie observed on HTTP was `HttpOnly`, `SameSite=Lax`, path `/`, and max age 28,800 seconds; it lacked `Secure`. The server identifies itself as Uvicorn. No CSP, frame, MIME-sniffing, referrer, permissions, HSTS or sensitive-page cache header was present. These observations map to OPS-SEC-002/011.

The application has no separate database listener, metrics endpoint, management interface or exposed Docker API. No unexpected Docker service was present. Swagger/OpenAPI is gated in the current live build but would be reachable with the rest of the application on unauthenticated public `main`.

### Reverse proxy and client identity

No nginx, Caddy, Traefik or Cloudflare layer was observed. Requests forwarded through Docker Desktop appeared in Uvicorn logs as bridge source `172.18.0.1`, which makes application source-rate limiting unsuitable as the sole client distinguisher. No unsafe trust of arbitrary `X-Forwarded-*` headers was found, but an eventual proxy deployment must explicitly define which proxy is trusted and strip inbound forwarding headers. Direct TCP/8000 should not remain as a bypass around that proxy.

### Live attack-surface conclusion

No live authentication bypass, source/config/database disclosure, broad CORS, debug traceback, unsafe HTTP verb or exposed administration service was found. The meaningful externally exploitable condition depends on network placement: if TCP/8000 is reachable by an untrusted LAN client, cleartext password/session transport, setup claiming on a fresh volume, lockout, and the application's authenticated capabilities become relevant. The published source is worse because it removes the authentication precondition entirely.

## 13. Positive Security Controls

The following controls were verified and should be preserved during remediation:

- Current/live middleware defaults to authentication for all paths except the minimal health, static, setup and login surfaces.
- State-changing HTML forms—including setup, login, logout, password change, settings, OAuth disconnect, pair creation/toggle/delete, baseline acceptance and Apply—use session-bound CSRF tokens.
- Passwords use random salts, memory-hard scrypt and constant-time verification; raw passwords are not stored.
- Session state is signed, HttpOnly, SameSite and time-limited; login clears pre-authentication state and password changes invalidate older sessions.
- OAuth state is random, compared safely and consumed once; provider tokens stay in backend code and are Fernet-encrypted at rest.
- Jinja autoescaping is enabled. No `safe`, `Markup`, `innerHTML`, `eval`, unsafe deserialization or user-controlled template compilation was found. Provider metadata is treated as ordinary escaped text in templates.
- SQLAlchemy constructs bound queries. No attacker-controlled raw SQL, shell/subprocess call, upload path or arbitrary file reader was found.
- Provider endpoints are fixed HTTPS hosts. No user-controlled fetch URL or practical SSRF/open-redirect sink was found.
- CORS is not enabled broadly. Health output is minimal, and docs/configuration UI are authenticated in the live build.
- First sync is additive; conflicts block; deletions require explicit review and an exact phrase; provider state is re-read; action outcomes are journaled; baselines advance only after results are observed.
- YouTube deletion uses exact playlist-item IDs. Scheduled synchronization produces previews rather than silently applying playlist writes.
- Container runtime is non-root, non-privileged, seccomp-confined and has zero effective/permitted capabilities, no Docker socket, no device/host mount and no database port.
- Repository and image contexts exclude normal secret/data paths; complete-history scanning found no secret.
- CI grants read-only repository permission, does not use production secrets or `pull_request_target`, and does not publish/deploy artifacts.

## 14. Untested / Partially Tested Areas

| Area | Why it was not fully verified | What is required |
|---|---|---|
| External/LAN reachability | Only the host itself was available; Windows withheld the specific firewall rule | A second authorized LAN client and router/firewall read access; passive port checks only |
| Public TLS/reverse proxy | No public hostname or proxy existed in the observed deployment | Intended hostname, TLS terminator and proxy/firewall configuration plus an external vantage point |
| GitHub private controls | Public API cannot reveal private repository settings/secrets | Read-only repository-administrator access/screenshots for branch, Actions, scanning, secret and environment policies |
| OAuth console settings | Spotify/Google developer consoles were not opened and secrets were intentionally not requested | Read-only console review of exact redirect URIs, app mode/test users, consent screen, scopes and revocation policy |
| OAuth replay/account switching | Could consume codes, alter connections or disrupt the user's current accounts | Disposable provider applications/accounts and isolated staging deployment |
| Login concurrency/rate limit | Brute-force/load behavior was prohibited and could lock out the user | Isolated staging, mocked password hash, bounded concurrency tests and resource telemetry |
| Apply races, retry and duplicate deletion | Could modify/destructively change real playlists | Disposable playlists/accounts in staging, fault injection and controlled concurrent requests |
| Large/malicious provider metadata | Live provider calls would consume quota | Recorded/synthetic provider fixtures covering size, Unicode and malformed/null data |
| Backups and recovery | Host backups/snapshots were not present | Sample backup inventory, encryption/ACL inspection and non-production restore exercise |
| Docker host/kernel isolation | Docker Desktop VM/kernel internals were outside scope; escape attempts prohibited | Patch/version inventory and a separate host-hardening review; no exploitation required |
| Other LAN services | Audit authorization concerned OPS; unrelated services were not probed | Explicitly expanded host/network scope if a broader infrastructure assessment is wanted |

These gaps do not invalidate the verified source/runtime findings. They should be closed during remediation verification before assigning a production-ready verdict.

## 15. Prioritized Remediation Plan

This plan is advisory only; no item was implemented during this audit.

### IMMEDIATE — before continued production use

| Action | Findings | Effort |
|---|---|---:|
| Commit/review the local authentication implementation, include all migrations/tests, make CI green, and build/deploy only from that immutable reviewed revision. Do not deploy public `main` as assessed. | OPS-SEC-001, OPS-SEC-014 | MEDIUM |
| Terminate HTTPS at a trusted proxy/VPN, eliminate direct TCP/8000 bypass, enable Secure cookies, configure exact callback origins, and verify from another LAN client. | OPS-SEC-002 | MEDIUM |
| Protect first-run initialization with an out-of-band one-time bootstrap secret or loopback/local provisioning and an atomic one-use claim. | OPS-SEC-003 | MEDIUM |
| Add a database-backed per-pair Apply lease and one-time plan approval bound to exact ordered provider items/versions; fail closed on provider drift. | OPS-SEC-006 | MEDIUM |
| Block automatic Spotify deletion where duplicate occurrence semantics are ambiguous until snapshot-bound behavior is proven with disposable playlists. | OPS-SEC-007 | SMALL |
| If the test administrator password previously shared in conversation is still active, change it after the secure HTTPS path is established. | Secret hygiene | SMALL |

### SHORT TERM

| Action | Findings | Effort |
|---|---|---:|
| Replace global lockout with atomic, progressive and concurrency-bounded throttling at both trusted proxy and application layers; add request/resource limits. | OPS-SEC-004 | MEDIUM |
| Convert Review initiation to a CSRF-protected POST/job, deduplicate per pair, bound provider work, cache results and expire old runs. | OPS-SEC-005 | MEDIUM |
| Redesign canonical identity to preserve Unicode and make mappings provenance-aware/pair-safe; migrate cautiously and require re-review/re-baselining. | OPS-SEC-008 | LARGE |
| Resolve and bind the Google channel identity, narrow/test scopes, pause pairs on account switches, clarify revoke/disconnect, and redact callback query strings from logs. | OPS-SEC-009, OPS-SEC-010 | MEDIUM |
| Upgrade `cryptography` to a fixed compatible release, rebuild on a patched base, scan the exact image and retain the reachability review. | OPS-SEC-013 | SMALL |
| Add controlled staging tests for duplicate occurrence, provider drift, retry, partial failure, multi-worker Apply and non-Latin metadata before enabling unattended Apply. | OPS-SEC-006–008 | LARGE |

### MEDIUM TERM

| Action | Findings | Effort |
|---|---|---:|
| Add CSP/nonces, frame, MIME, referrer, permissions, cache and TrustedHost controls; enable HSTS only at the HTTPS edge. | OPS-SEC-011 | SMALL |
| Enable SQLite foreign keys on every connection; tighten data/database ACLs; design key separation, rotation and protected backup/restore. | OPS-SEC-012 | MEDIUM |
| Apply explicit Docker containment: all capabilities dropped, no-new-privileges, read-only root/tmpfs, minimal runtime contents, and CPU/memory/PID limits. | OPS-SEC-013 | MEDIUM |
| Pin Actions/tool/base references immutably; add hash-checked locks, dependency updates, SAST/secrets/image scans, SBOM/provenance, `SECURITY.md`, and protected green branch requirements. | OPS-SEC-014 | MEDIUM |
| Add run retention, audit events and visible provider-quota/work estimates without logging OAuth material or sensitive playlist metadata. | OPS-SEC-005, OPS-SEC-009 | MEDIUM |

### OPTIONAL HARDENING

| Action | Findings | Effort |
|---|---|---:|
| Add PKCE to Spotify's confidential flow as defense in depth and test exact redirect/state/code-replay behavior. | OAuth defense in depth | MEDIUM |
| Move encryption/session keys to a dedicated secret source and support envelope/key rotation with recovery-safe versioning. | OPS-SEC-012 | LARGE |
| Restrict container egress to required DNS/Spotify/Google destinations if the network platform supports maintainable policy. | OPS-SEC-013 | MEDIUM |
| Prefer VPN/private-access placement and add privacy-preserving alerts for repeated login failures, unexpected OAuth reconnects and large deletion plans. | OPS-SEC-002/004/006 | MEDIUM |
| Establish encrypted backup retention and periodic non-production restore/reconciliation drills. | Storage/integrity resilience | MEDIUM |

## 16. Final Verdict

**Verdict: NOT READY FOR PRODUCTION.**

1. **Is Open Playlist Sync currently safe enough for production?**  
   **No.** The published deployable branch has no authentication, and the live build is exposed over cleartext HTTP with uncommitted security changes. The immediate plan must be completed and independently verified first.

2. **Is it safe to connect real Spotify and YouTube/Google accounts?**  
   **Not to the assessed public release or current cleartext network deployment.** The live code protects tokens reasonably at the application layer, but an unauthenticated published instance or stolen HTTP session can use their authority. Use disposable/low-value playlists until authenticated immutable deployment, HTTPS and high-risk sync controls are verified.

3. **Could OAuth tokens realistically be exposed?**  
   **Yes under host/volume/process compromise, but no direct web token-disclosure route was found.** Tokens are encrypted and absent from pages/current logs; however, the database and key share one volume, and any full application compromise can decrypt/use them. One authorization code/state appeared in access logs, not a refresh/access token.

4. **Could an attacker cause unauthorized playlist modifications?**  
   **Yes.** Any client reaching a deployment from public `main` can act through stored provider credentials. On the live build this requires taking the password/session, winning first-run setup, or exploiting an authenticated operation/race; no direct live auth bypass was found.

5. **Could application bugs cause destructive playlist synchronization?**  
   **Yes.** The review safeguards reduce probability, and current live pairs have no accepted baselines, but concurrent/replayed Apply, Spotify duplicate deletion ambiguity, and canonical/mapping collisions can cause unintended changes after baseline acceptance.

6. **Are there externally exploitable vulnerabilities?**  
   **Yes if the service port is reachable:** public `main` has direct unauthenticated control; live/default deployment exposes cleartext credentials/sessions to on-path actors, fresh setup can be claimed, and login can be cheaply locked. No externally reachable RCE, SQLi, XSS, SSRF, path traversal or container-management interface was found.

7. **Is the Docker deployment adequately isolated?**  
   **Partially.** Non-root execution, zero effective capabilities, seccomp and absence of host/socket mounts are strong. All-interface HTTP exposure, writable root, missing no-new-privileges/limits, broad egress and key/data co-location prevent an adequate production rating.

8. **Could container compromise meaningfully endanger the host?**  
   **It would seriously endanger connected accounts and application data, but no easy host-control path was found.** There is no Docker socket, privileged mode, host mount, device or host network. Host impact would depend on an additional Docker/kernel vulnerability or network pivot; that was neither identified nor attempted.

9. **Are any credentials/secrets currently exposed or in need of rotation?**  
   **No committed/current OAuth or client secret exposure was found, so no immediate provider rotation is supported by evidence.** Change the previously shared test administrator password if it remains active. Redact future callback queries and separately protect database backups and keys.

10. **What are the five highest-priority security improvements?**

    1. Publish and deploy the authenticated code from a reviewed, immutable, green-CI revision.
    2. Enforce HTTPS/private access and remove direct all-interface HTTP bypass.
    3. Secure first-run bootstrap and replace race-prone/global login lockout.
    4. Serialize Apply and bind one-time approval to exact provider snapshots; block unsafe Spotify duplicate deletion meanwhile.
    5. Replace ASCII-destructive canonical identity and global overwrite-prone mappings with Unicode-safe, provenance-aware reconciliation.

### Critical/High validation note

All Critical/High candidates were re-reviewed for reachability, attacker control, framework mitigation, authentication, production configuration and actual feature use. The only surviving High is the verified absence of authentication in public `main`. The live authentication gate disproves that condition for the current container but does not remediate the reproducible public release. Docker scanner Critical/High advisories were downgraded from application findings because OPS does not invoke their vulnerable Perl, OpenSSL CMS/CMP/DTLS or cryptography PKCS#7 paths. No duplicate finding or unredacted secret remains in this report, and no production state was modified during the audit.
