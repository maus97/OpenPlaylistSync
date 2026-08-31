FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPS_STATIC_DIR=/app/static \
    OPS_TEMPLATES_DIR=/app/templates

WORKDIR /app

RUN addgroup --system ops && adduser --system --ingroup ops ops

COPY pyproject.toml README.md LICENSE requirements.lock ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
COPY templates ./templates
COPY static ./static

RUN pip install --no-cache-dir -r requirements.lock . \
    && mkdir -p /data \
    && chown -R ops:ops /app /data

USER ops

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn ops.main:app --host 0.0.0.0 --port 8000"]
