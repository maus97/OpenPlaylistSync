# Open Playlist Sync (OPS)

Open Playlist Sync is an open-source, privacy-first, self-hosted service for
bidirectional playlist synchronization. The initial provider targets are
Spotify and YouTube Music.

This repository contains a runnable, operator-assisted application. Provider
credentials and sync settings can be entered from the local web UI; playlist
pairs can be selected in the browser, and every write remains guarded by plan
review and explicit safety checks.

## Design commitments

- Provider-neutral synchronization engine with Spotify and YouTube Music behind
  a common interface.
- SQLite persistence through SQLAlchemy and Alembic.
- Docker-first deployment with persistent volume support.
- No telemetry and no hardcoded credentials.
- Credentials encrypted at rest before they are persisted.
- First-run local administrator password, signed sessions, and brute-force lockout.
- Non-destructive initial synchronization.
- Three-way reconciliation against the previous successful baseline.
- Explicit safety checks before destructive actions.
- Mockable provider APIs and synthetic synchronization tests.
- Operator-assisted OAuth boundaries for Spotify and YouTube Music.
- Server-rendered dashboard and synchronization-run history.

Read the [implementation architecture](docs/architecture.md) for the module
boundaries, data model direction, reconciliation model, deployment approach,
and decisions that remain open. The
[technical completion guide](docs/technical-completion-guide.md) provides the
ordered roadmap, release gates, and definition of done for taking the current
milestone to dependable real-world synchronization.

For browser setup instructions, open **Settings** in OPS and select a provider
title for its detailed guide. For server and Docker deployment instructions,
read the [deployment guide](docs/deployment.md).

## Local development

The target runtime is Python 3.12. The commands below use `uv` to create and
manage the project environment:

```powershell
uv sync --extra dev
uv run uvicorn ops.main:app --reload
```

The scaffold exposes a health endpoint:

```text
http://127.0.0.1:8000/healthz
```

Run the quality checks with:

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

## Database

Copy `.env.example` to `.env` for local configuration. The default database is
`data/ops.db`, which is ignored by Git. Apply the initial schema with:

```powershell
uv run alembic upgrade head
```

The migration establishes the persistence boundary, encrypted credential
storage, and encrypted GUI configuration storage. Provider authentication is
started from `/settings` and `/pairs`; synchronization still requires the
operator's review and approval flow.

## Docker

Build and run the service with:

```powershell
docker compose up --build
```

The Compose file stores SQLite data in the named `ops-data` volume and exposes
port `8000`. The container runs as a non-root user.

For a normal server, change the public hostname and redirect address in the
provider dashboards before connecting accounts. See the deployment guide for
the reverse-proxy, HTTPS, backup, and update steps.

## Repository status

Implemented:

- FastAPI application factory and `/healthz`.
- SQLAlchemy base, initial persistence models, and Alembic configuration.
- Provider-neutral contracts plus Spotify and YouTube Music adapter seams.
- Pure three-way reconciliation plans with conflict and destructive-action
  safety checks.
- Fernet credential encryption boundary and SQLAlchemy repositories.
- Provider adapters with guarded writes and operator-assisted authentication flows.
- APScheduler lifecycle boundary and a server-rendered operator dashboard with
  playlist-pair configuration, plan review, and explicit approval flows.
- First-sync choices for merge, source-led, target-led, or accept-as-is setup;
  all initial convergence actions are additions only.
- Occurrence-aware snapshots, provider paging, confidence-gated destination
  searches, access-token refresh, and exact YouTube Music playlist-item removals.
- Durable plan/action journal, CSRF-protected browser forms, and pair
  pause/delete/disconnect controls.
- A simple browser UI for provider setup, account connections, playlist creation,
  pairing, plan review, and run history.
- Python packaging, pytest, Ruff, Docker, Compose, environment template, and
  GitHub Actions CI.

Before using valuable real playlists:

- Complete the browser-based provider setup below and test with disposable
  playlists first.
- Review ambiguous songs rather than relying on an automatic match.
- Keep remote deployments behind authenticated HTTPS access; OPS is designed
  for local or trusted-network use by default.

## Production setup

For real providers, open `/settings`; the Spotify and YouTube Music titles link
to detailed first-time setup guides. OPS automatically creates local session and encryption keys in the persistent
data directory when deployment values are not supplied. The optional `.env`
file is intended for deployment overrides, not normal operator setup.

After saving settings, open `/pairs`, connect Spotify and YouTube Music, choose
playlists, select an initial-sync policy, and review the first non-destructive
plan. OPS saves the baseline only after the reviewed initial state is complete.
Review each later plan before applying it.

### Provider access setup

The same instructions are available from the provider titles on the **Settings**
page. No provider passwords are entered into OPS; the provider authorization
pages handle account approval.

#### Spotify

1. Open the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
   and create an app.
2. In the app settings, add this exact redirect URI:
   `http://127.0.0.1:8000/auth/spotify/callback`.
3. Copy the app's Client ID and Client Secret into OPS Settings.
4. Save the settings, open Pairs, choose **Connect Spotify**, and approve the
   requested access.

OPS requests `user-read-private`, `playlist-read-private`,
`playlist-read-collaborative`, `playlist-modify-private`, and
`playlist-modify-public` so it can search the Spotify catalogue and read or
write private, public, and collaborative playlists. Spotify requires the
loopback URI to match exactly and uses `127.0.0.1`, not `localhost`; see the
[redirect URI rules](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri)
and [playlist scopes](https://developer.spotify.com/documentation/web-api/concepts/scopes).

#### YouTube Music

1. In [Google Cloud Console](https://console.cloud.google.com/projectcreate),
   create or select a project.
2. Enable [YouTube Data API v3](https://console.cloud.google.com/apis/library/youtube.googleapis.com).
3. In [Credentials](https://console.cloud.google.com/apis/credentials), create
   an OAuth client ID and choose **TVs and Limited Input devices**.
4. Copy the client ID and secret into OPS Settings and save.
5. Open Pairs and choose **Connect YouTube Music**. OPS will show a Google
   verification URL and one-time code. Complete the approval, return to OPS,
   and choose **I completed authorization**.

OPS uses the Google device-code approval flow with the
`https://www.googleapis.com/auth/youtube` read/write scope. Playlist discovery,
matching, creation, additions, and removals use the official
[YouTube Data API v3](https://developers.google.com/youtube/v3), rather than
private YouTube Music endpoints. Google Cloud's
[authorization credential guide](https://developers.google.com/youtube/registering_an_application)
explains the OAuth credential types.

## License

OPS is distributed under the [GNU Affero General Public License v3 or later](LICENSE).

AGPL keeps modified versions open and requires source access for users interacting with modified network deployments. It does not prohibit selling copies or charging for hosting or support; a no-commercial-use restriction would not meet the open-source definition.
