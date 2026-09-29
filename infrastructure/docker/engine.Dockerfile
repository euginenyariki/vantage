# syntax=docker/dockerfile:1
# Vantage trading engine: supervisor loop, reconciliation and backtest workers.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

COPY apps/trading-engine/pyproject.toml ./pyproject.toml
COPY apps/trading-engine/trading_engine/__init__.py ./trading_engine/__init__.py

RUN pip install --upgrade pip && pip install .

COPY apps/trading-engine /build

RUN useradd --create-home --shell /usr/sbin/nologin vantage \
 && chown -R vantage:vantage /build
USER vantage

ENV PYTHONPATH=/build

# Paper-first: the supervisor refuses to run a live bot without an explicit
# per-bot grant, and LIVE_TRADING_ENABLED defaults to false.
CMD ["python", "-m", "trading_engine.main"]
