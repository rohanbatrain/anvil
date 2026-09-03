# Multi-stage: build wheels once, ship a slim runtime.
FROM python:3.12-slim AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY anvil ./anvil
RUN pip install --no-cache-dir --upgrade pip build \
    && pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 anvil
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels anvil && rm -rf /wheels
COPY --chown=anvil:anvil anvil ./anvil
COPY --chown=anvil:anvil alembic ./alembic
COPY --chown=anvil:anvil alembic.ini ./
USER anvil
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
  CMD curl -fsS "http://localhost:${PORT:-8000}/health" || exit 1
# Shell form so ${PORT} expands. Render, Railway and Heroku inject it; Fly
# does not, hence the default.
CMD ["sh", "-c", "exec uvicorn anvil.main_api:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
