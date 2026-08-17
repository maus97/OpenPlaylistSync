# Open Playlist Sync contributor guidance

## Project intent

Open Playlist Sync (OPS) is an open-source, privacy-first, self-hosted
bidirectional playlist synchronization service. The first provider adapters are
Spotify and YouTube Music.

The application is designed to run locally or in Docker on TrueNAS SCALE, but
must not depend on TrueNAS-specific APIs or services.

## Boundaries

- Keep provider-specific API calls inside `src/ops/providers/`.
- Keep synchronization policy provider-neutral in `src/ops/sync/`.
- Treat the last successful synchronization as the immutable baseline for
  three-way reconciliation.
- Initial synchronization must be non-destructive. Deletes require an
  explicit safety decision and a second validation step.
- Never add telemetry, analytics, hardcoded credentials, or provider secrets to
  the repository.
- Credentials must be encrypted before persistence. Do not store access tokens
  or refresh tokens in plaintext columns, logs, fixtures, or snapshots.
- Provider clients must be injectable or mockable so synchronization tests do
  not contact real services.
- Prefer server-rendered Jinja2 and small HTMX interactions over a separate SPA
  unless an architecture decision records a reason to change this.

## Development commands

```powershell
uv sync --extra dev
uv run ruff format .
uv run ruff check .
uv run pytest
uv run uvicorn ops.main:app --reload
```

The development health check is `http://127.0.0.1:8000/healthz`.

## Change expectations

Before changing the data model, update the architecture documentation and add
an Alembic migration. Before adding a provider operation, update the common
provider contract and add synthetic tests for the provider-neutral behavior.
Keep changes small and explain any security or destructive-action implications
in the pull request.
