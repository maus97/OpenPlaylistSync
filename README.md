# Open Playlist Sync (OPS)

Open Playlist Sync is an open-source, privacy-first, self-hosted service for
bidirectional playlist synchronization. The initial provider targets are
Spotify and YouTube Music.

This repository contains the implementation architecture and a runnable
operator-assisted application milestone. Provider credentials and sync settings
can be entered from the local web UI; writes remain guarded by plan review and
explicit safety checks.

## Design commitments

- Provider-neutral synchronization engine with Spotify and YouTube Music behind
  a common interface.
- SQLite persistence through SQLAlchemy and Alembic.
- Docker-first deployment with a portable TrueNAS SCALE path.
- No telemetry and no hardcoded credentials.
- Credentials encrypted at rest before they are persisted.
- Non-destructive initial synchronization.
- Three-way reconciliation against the previous successful baseline.
- Explicit safety checks before destructive actions.
- Mockable provider APIs and synthetic synchronization tests.
- Operator-assisted OAuth boundaries for Spotify and YouTube Music.
- Server-rendered dashboard and synchronization-run history.

Read the [implementation architecture](docs/architecture.md) for the module
boundaries, data model direction, reconciliation model, deployment approach,
and decisions that remain open.

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
- Offline demo workspace for testing baseline creation, simulated additions and
  removals, conflict review, and safe application without provider credentials.
- Python packaging, pytest, Ruff, Docker, Compose, environment template, and
  GitHub Actions CI.

Remaining for production hardening:

- Live provider end-to-end verification and rate-limit/retry tuning.
- Track ordering, duplicate handling, and richer metadata reconciliation.
- Credential key rotation and recovery workflow.

## Production setup

For immediate testing, open `/` and choose **Try local demo**. For real
providers, open `/settings` and enter the OAuth client values through the GUI.
OPS automatically creates local session and encryption keys in the persistent
data directory when deployment values are not supplied. The optional `.env`
file is intended for deployment overrides, not normal operator setup.

After saving settings, open `/pairs`, connect Spotify and YouTube Music, choose
playlists, and establish the first non-destructive baseline. Review each later
plan before applying it.
