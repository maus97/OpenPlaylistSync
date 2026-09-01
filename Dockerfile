ARG PYTHON_IMAGE=python:3.12.14-slim-trixie@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217

FROM ${PYTHON_IMAGE} AS wheel-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build

COPY requirements-build.lock ./
RUN python -m pip install --require-hashes --no-deps -r requirements-build.lock

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels .

FROM ${PYTHON_IMAGE} AS dependency-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
COPY requirements.lock /tmp/requirements.lock
RUN /opt/venv/bin/python -m pip install --require-hashes -r /tmp/requirements.lock
COPY --from=wheel-builder /wheels /wheels
RUN /opt/venv/bin/python -m pip install --no-deps /wheels/open_playlist_sync-*.whl \
    && /opt/venv/bin/python -m pip uninstall --yes pip setuptools wheel

FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="Open Playlist Sync" \
      org.opencontainers.image.source="https://github.com/maus97/OpenPlaylistSync" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPS_STATIC_DIR=/app/static \
    OPS_TEMPLATES_DIR=/app/templates

RUN python -m pip uninstall --yes pip setuptools wheel \
    && addgroup --system --gid 10001 ops \
    && adduser --system --uid 10001 --ingroup ops --home /nonexistent --no-create-home ops \
    && mkdir -p /app /data /run/ops-secrets \
    && chown ops:ops /data /run/ops-secrets \
    && chmod 0700 /data /run/ops-secrets

WORKDIR /app
COPY --from=dependency-builder --chown=root:root /opt/venv /opt/venv
COPY --chown=root:root migrations ./migrations
COPY --chown=root:root alembic.ini ./
COPY --chown=root:root templates ./templates
COPY --chown=root:root static ./static

USER 10001:10001
EXPOSE 8000

CMD ["python", "-m", "ops.entrypoint"]
