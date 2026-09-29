# syntax=docker/dockerfile:1
# Vantage API: FastAPI + Alembic.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# curl backs the container healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only the manifests first so the dependency layer is cached across
# source edits.
COPY apps/api/pyproject.toml ./api/pyproject.toml
COPY apps/trading-engine/pyproject.toml ./engine/pyproject.toml
COPY apps/api/app/__init__.py ./api/app/__init__.py
COPY apps/trading-engine/trading_engine/__init__.py ./engine/trading_engine/__init__.py

RUN pip install --upgrade pip \
 && pip install ./engine ./api

# Now the real source.
COPY apps/api /build/api
COPY apps/trading-engine /build/engine
RUN pip install --no-deps ./engine ./api

ENV PYTHONPATH=/build/api:/build/engine \
    PATH="/usr/local/bin:${PATH}"

WORKDIR /build/api

RUN useradd --create-home --shell /usr/sbin/nologin vantage \
 && chown -R vantage:vantage /build
USER vantage

EXPOSE 8000

# The app serves the API under /api, so the probe path includes the prefix.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
