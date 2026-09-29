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
