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

## No container registry, CI, or local-image workflow

Docker is not available on the development machine, so nothing here has been
built into a container locally; the images are built in ACR by
`infrastructure/azure/deploy.sh`. There is no CI pipeline, so the 288 tests are
only run by hand. The web app has no unit tests at all, only typecheck and build.

## Deployed data stores are publicly reachable

PostgreSQL and Redis are deployed with public endpoints. The database relies on
the "allow Azure services" firewall rule, which permits any Azure resource on the
internet to attempt a connection, and is authenticated only by a password. Redis
is behind an access key but the same network exposure applies.

The upgrade path is a private endpoint plus VNet integration on the Container Apps
environment. That is the right shape before any real money or real user data is
involved.
