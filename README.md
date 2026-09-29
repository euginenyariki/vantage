# Vantage

An automated trading platform: run strategies against a broker, watch them live, and
replay them over history before risking anything.

**This software does not make money.** It is a tool for testing and monitoring trading
logic. Every strategy in it is unproven, and the built-in market data is synthetic until
you connect a real broker. Nothing here is financial advice.

---

## What it is

| Piece | Stack | Role |
|---|---|---|
| `apps/web` | Next.js 15, React 19, Tailwind | Dashboard |
| `apps/api` | FastAPI, SQLAlchemy 2, Alembic | REST + WebSocket API |
| `apps/trading-engine` | Python, asyncio | Strategy execution, risk, reconciliation, backtests |

Backing services: PostgreSQL for state, Redis for the event bus and job queue.

## Features

- **Multi-strategy execution** — HFT scalper and mean-reversion scalper, registered in a
  strategy registry and selectable per bot.
- **Risk engine** — per-trade sizing, daily loss limits, exposure caps, drawdown
  circuit breakers, and a kill switch that works even when the engine is wedged.
- **Backtesting** — replay a strategy over historical bars with spread, slippage, and
  commission applied. Results include the equity curve, drawdown, and every trade.
- **Broker integration** — MetaApi for MT4/MT5 accounts; paper trading needs no broker.
- **Realtime dashboard** — bot state, positions, equity, and P/L over a WebSocket.
- **Multi-tenant by construction** — every query is scoped by `user_id`, including
  backtest results and realtime events.

## Safety model

Live trading is off unless three independent gates all pass. Any one of them alone is
insufficient, so all three are required:

1. The server runs with `LIVE_TRADING_ENABLED=true`.
2. The specific bot was explicitly granted live trading (`live_granted`).
3. The connected broker account is not a demo account.

The shipped defaults are `TRADING_MODE=PAPER` and `LIVE_TRADING_ENABLED=false`. The API
refuses to start in production while the development secret placeholders are still in
place.

## Quick start

Requires Docker with Compose v2.

```bash
cp .env.example .env
# Set the three required secrets. Generate them, do not invent them:
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

docker compose up -d
```

That starts Postgres, Redis, the API, the web dashboard, the trading engine, and the
backtest worker. The API container applies migrations before serving, so there is no
separate migration step. To add the nginx edge proxy, add its profile:

```bash
docker compose --profile edge up -d
```

The dashboard is then on `http://localhost:3000` and the API on
`http://localhost:8000` (interactive docs at `/docs`).

### Running without Docker

Postgres and Redis must be reachable; the Python apps are otherwise self-contained.

```bash
python3 -m venv apps/api/.venv
apps/api/.venv/bin/pip install -e apps/trading-engine -e 'apps/api[dev]'

cd apps/api
../api/.venv/bin/alembic upgrade head
../api/.venv/bin/uvicorn app.main:app --reload
```

`CORS_ORIGINS` accepts a bare URL, a comma-separated list, or a JSON array.

## Tests

```bash
apps/api/.venv/bin/python -m pytest apps/api        # 140 tests
apps/api/.venv/bin/python -m pytest apps/trading-engine  # 148 tests
cd apps/web && npm run typecheck && npm run build
```

Run both Python suites from the repository root using the API venv's interpreter. One
venv covers both packages; a relative path to the venv's binary from inside another
directory triggers a spurious `sys.prefix` warning.

The API suite runs against a real `vantage_test` database and a separate Redis logical
DB, so routers, ORM models, and session handling are exercised rather than mocked. No
test contacts a real broker.

## How a backtest runs

1. The client posts to `POST /api/backtests`. The job is written to PostgreSQL and its id
   is published to the Redis `backtest` channel.
2. `BacktestWorker` receives the id, reloads the full job from the database, fetches
   bars, and simulates bar by bar.
3. Progress and completion are published to the event bus; the dashboard listens and
   refetches on completion.
4. The result — metrics, curves, and every trade — is stored and returned by
   `GET /api/backtests/{id}`.

PostgreSQL is the source of truth for job state, not Redis. Because Redis pub/sub is
fire-and-forget, the worker reconciles any job left in `QUEUED` by a previous run at
startup; otherwise a restart would strand queued work permanently.

Intra-bar fills are deliberately pessimistic: when one bar's range contains both the
stop and the target, the stop is assumed to have been hit first.

## Layout

```
apps/
  api/            FastAPI service
    app/routers/  auth, bots, brokers, backtests, analytics, trading, ws, admin
    tests/
  trading-engine/
    trading_engine/
      strategies/ strategy registry and implementations
      risk/        sizing, limits, circuit breakers
      execution/   order lifecycle, event bus
      backtest/    bar-by-bar simulation engine
      brokers/     paper and MetaApi adapters
    tests/
  web/            Next.js dashboard
infrastructure/   Dockerfiles and edge config
docs/             known-gaps and design notes
```

## Configuration

Every setting is an environment variable; see `.env.example` for the annotated list.
The ones that matter most:

| Variable | Default | Notes |
|---|---|---|
| `TRADING_MODE` | `PAPER` | `PAPER`, `DEMO`, or `LIVE` |
| `LIVE_TRADING_ENABLED` | `false` | Server-side gate for live trading |
| `METAAPI_TOKEN` | *(empty)* | Required to reach a real MT4/MT5 account |
| `CREDENTIAL_MASTER_KEY` | *(dev placeholder)* | Encrypts broker credentials at rest |
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated or JSON array |

Rotating `CREDENTIAL_MASTER_KEY` invalidates every stored broker credential. Plan a
re-encryption before you rotate it.

## Honest limitations

- **No strategy here has been shown to be profitable.** The bundled market data is a
  random walk, and a backtest over synthetic data measures the code, not the edge.
- Backtests assume fills at bar prices. Real slippage, requotes, and partial fills are
  not modelled, so results are optimistic by construction.
- Live execution against MT4/MT5 depends on MetaApi and the broker's own execution
  quality. The platform cannot guarantee a fill.
- No tax, financing, or currency-conversion modelling. Only commission, spread, and
  slippage are applied.
- There is **no account lockout** and **no mailer**: login is throttled per IP only, and
  email verification and password reset are not implemented.

`docs/known-gaps.md` records the specifics.
