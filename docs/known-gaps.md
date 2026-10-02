# Known gaps

Recorded 2026-09-29, after a full-stack verification pass. Each item is a real
absence, not a task in progress.

## Account lockout does not exist

`ACCOUNT_LOCKOUT_*` was removed from `.env.example` because nothing read it. What
actually protects the login endpoint is a per-IP rate limit, hardcoded in
`apps/api/app/routers/auth.py`:

| Route | Limit | Window |
|---|---|---|
| `auth-login` | 10 | 60s |
| `auth-register` | 5 | 3600s |
| `auth-refresh` | 60 | 60s |

A distributed attacker rotating source addresses gets no lockout, and a legitimate
user behind shared NAT can be denied service by one attacker. Fixing this means
tracking failed logins per account in PostgreSQL and expiring them on a schedule.
Rate limits are also not configurable, so they cannot be tuned per environment.

## No mailer

`EMAIL_*` was removed from `.env.example` for the same reason: there is no email
module. Consequences:

- Email verification is not enforced, so an address is never proven to exist.
- Password-reset emails cannot be sent.
- Any user-supplied email is unverified input.

This matters for a trading product, where account recovery usually goes through email.
Password reset is currently unavailable; recovery is manual via an operator.

## Market data is synthetic

The bundled bar generator is a random walk, and the MetaApi feed is not wired into
the backtest path. Backtest results therefore measure the code, not an edge. Treat
every number produced locally as a smoke test, not evidence.

## MetaApi adapter is unverified against a live account

The adapter was written against MetaApi's published OpenAPI spec and its paths,
payloads, region hosts and `auth-token` header were checked against the live
service, but **no request has been made with a real token and a real account**.
Everything is proven only against recorded payloads and the spec.

Two specific problems were found while verifying it, both outside our control:

- **The provisioning API does not present a valid certificate.**
  `mt-provisioning-api-v1.<region>.agiliumtrade.ai` serves a self-signed
  "Kubernetes Ingress Controller Fake Certificate", so TLS verification fails and
  `provision_account()` and `create_demo_account()` cannot be called from here.
  The client and market-data hosts are unaffected. Until MetaApi fixes this,
  accounts must be added in the MetaApi web app and the resulting `account_id`
  passed to the adapter, which skips provisioning entirely. Worth reporting to
  support@metaapi.cloud.
- **Historical candles can wedge the terminal.** MetaApi documents a bug where
  requesting candles for a symbol the broker has no data for sends the MT5
  terminal into an infinite loop, and the account must be redeployed to recover.
  `get_bars` therefore only requests symbols the account actually reports via
  `get_symbols`, but a symbol can still lose its history later.

There is no MetaApi sandbox. `create_demo_account()` is the closest equivalent:
it asks MetaApi to generate a real MT5 demo account at a demo broker server and
provision a terminal for it, which gives real order mechanics with no financial
exposure. It still requires a paid MetaApi token and bills for terminal runtime,
so it is risk-free on the money but not on the cost.

## No container registry, CI, or local-image workflow

Docker is not available on the development machine, so nothing here has been
built into a container locally; the images are built in ACR by
`infrastructure/azure/deploy.sh`. There is no CI pipeline, so the 315 tests are
only run by hand. The web app has no unit tests at all, only typecheck and build.

## Deployed data stores are publicly reachable

In `MODE=managed`, PostgreSQL and Redis are deployed with public endpoints. The
database relies on the "allow Azure services" firewall rule, which permits any
Azure resource on the internet to attempt a connection, and is authenticated only
by a password. Redis is behind an access key but the same network exposure
applies.

The upgrade path is a private endpoint plus VNet integration on the Container Apps
environment. That is the right shape before any real money or real user data is
involved.

In `MODE=demo` this does not apply: both stores run as container apps with
`external: false` ingress and are reachable only from inside the environment. The
trade there is durability, not exposure.

## Demo mode has no durable storage

`MODE=demo` does not keep data. It is not the default, but it is available and
worth understanding before using it. A consumption-only
Container Apps environment cannot attach a persistent volume, and Azure Files is
not a workaround — mounting a share at the Postgres data directory corrupts the
cluster, because SMB does not give Postgres the byte-range locking it requires.

So the database directory is ephemeral container storage. The next redeploy, image
update, or scale-in of `vantage-pg` deletes every user, bot, order, and backtest,
with no recovery and no way to restore one. There are no backups in this mode.

This is acceptable for a URL someone can click. It is not acceptable for anything
where an account, a strategy, or a backtest result needs to still be there
tomorrow. `MODE=managed` fixes it at roughly $28/month, and it is the mode to use
before real users are involved.
