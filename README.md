# Open Playlist Sync (OPS)

Open Playlist Sync is an open-source, privacy-first, self-hosted service for
bidirectional playlist synchronization. The initial provider targets are
Spotify and YouTube Music.

This repository contains the implementation architecture and the first runnable
application milestone. Provider writes and full synchronization execution remain
intentionally guarded while the reconciliation and credential boundaries are
validated.

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

The migration establishes only the persistence boundary and encrypted-secret
storage shape. It does not authenticate with Spotify or YouTube Music and does
not synchronize playlists.

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
- Read-only provider adapters and operator-assisted authentication flows.
- APScheduler lifecycle boundary and a server-rendered operator dashboard with
  playlist-pair configuration, plan review, and explicit approval flows.
- Python packaging, pytest, Ruff, Docker, Compose, environment template, and
  GitHub Actions CI.

Not implemented yet:

- Provider playlist writes and synchronization execution.
- Credential key rotation and recovery workflow.

## Next implementation milestone

The next milestone is the provider-neutral reconciliation engine, followed by
encrypted credential storage and read-only provider adapters. Live provider
authentication requires operator-created OAuth applications and credentials;
those values belong in a local secret manager or ignored `.env` file, never in
Git.
