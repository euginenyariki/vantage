'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';

import { marketApi } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import type { SymbolInfo } from '@/lib/types';

const FEATURES = [
  {
    icon: '⛨',
    title: 'Risk engine before the broker',
    body: 'Every order passes the risk engine first. Per-trade risk sizing, daily and weekly loss caps, spread limits, and a kill switch that halts every bot immediately.',
  },
  {
    icon: '◔',
    title: 'Performance you can audit',
    body: 'Expectancy, profit factor, payoff ratio, max drawdown and PnL-based Sharpe, computed from your own closed trades — broken down by market and by strategy, with unavailable metrics shown as unavailable.',
  },
  {
    icon: '⇄',
    title: 'MT4 and MT5 via MetaApi',
    body: 'Connect a real account and the same code path that runs your paper bots takes over. Credentials are encrypted at rest and never returned by the API.',
  },
  {
    icon: '⟳',
    title: 'Paper, demo, then live',
    body: 'Simulated data exercises the identical execution path. Live mode stays locked behind an explicit grant that you control and can revoke at any time.',
  },
  {
    icon: '◈',
    title: 'Every fill reconciled',
    body: 'The engine reconciles broker balances and positions against its own records on a schedule and flags any disagreement rather than silently trusting one side.',
  },
  {
    icon: '▤',
    title: 'An audit trail on every change',
    body: 'Migrations, snapshots and an audit record of every state change, so any trade or permission change can be explained after the fact.',
  },
];

const GUARDRAILS = [
  'No guaranteed returns. Every performance figure on this platform is measured from your own trade history, never projected.',
  'Paper results are a rehearsal of execution and risk control, not evidence of live profitability.',
  'Binary and exotic products are not offered. Instruments listed here are the ones the engine actually prices and sizes.',
  'Vantage is not a broker, does not hold your funds, and does not give investment advice.',
];

export default function LandingPage() {
  const { user, ready } = useAuth();
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const [symbols, setSymbols] = useState<SymbolInfo[] | null>(null);

  useEffect(() => {
    marketApi
      .health()
      .then((h) => setHealthy(h.status === 'ok'))
      .catch(() => setHealthy(false));
  }, []);

  useEffect(() => {
    // The instrument count is read from the API rather than hard-coded, so the
    // marketing page cannot claim coverage the engine does not have.
    marketApi
      .symbols()
      .then(setSymbols)
      .catch(() => setSymbols([]));
  }, []);

  return (
    <div className="hero-bg min-h-dvh">
      <div className="pointer-events-none fixed inset-0 grid-overlay" aria-hidden />

      <header className="sticky top-0 z-40 border-b border-edge bg-base-950/70 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4 sm:px-6">
          <span className="text-lg font-semibold tracking-tight text-slate-50">
            vantage<span className="text-brand-400">.</span>
          </span>
          <nav className="flex items-center gap-2">
            {ready && user ? (
              <Link href="/dashboard" className="btn-primary">
                Dashboard
              </Link>
            ) : (
              <>
                <Link href="/login" className="btn-ghost hidden sm:inline-flex">
                  Sign in
                </Link>
                <Link href="/register" className="btn-primary">
                  Get started
                </Link>
              </>
            )}
          </nav>
        </div>
      </header>

      <main className="relative">
        <section className="mx-auto max-w-6xl px-4 py-16 sm:px-6 sm:py-24">
          <div className="max-w-3xl">
            <p className="mb-4 inline-flex items-center gap-2 rounded-full border border-edge bg-base-900/70 px-3 py-1 text-xs font-medium text-brand-400">
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  healthy === null
                    ? 'bg-slate-500'
                    : healthy
                      ? 'bg-up'
                      : 'bg-amber-400'
                }`}
                aria-hidden
              />
              {healthy === null
                ? 'Checking platform status'
                : healthy
                  ? 'All systems operational'
                  : 'Platform degraded'}
            </p>
            <h1 className="text-4xl font-bold leading-[1.1] tracking-tight text-gradient sm:text-5xl lg:text-6xl">
              Automated trading, inside a risk envelope you set.
            </h1>
            <p className="mt-5 max-w-2xl text-base leading-relaxed text-slate-400 sm:text-lg">
              Vantage runs your strategies against FX, metals and crypto CFDs with
              per-trade risk limits, broker-confirmed execution, and continuous
              reconciliation. Paper trade the entire time — then decide whether the
              numbers justify going live.
            </p>
            <div className="mt-9 flex flex-col gap-3 sm:flex-row">
              <Link href="/register" className="btn-primary px-6 py-3 text-base">
                Create free account
              </Link>
              <Link href="/login" className="btn-ghost px-6 py-3 text-base">
                Sign in
              </Link>
            </div>

            <dl className="mt-12 grid max-w-xl grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-4">
              <div>
                <dt className="text-xs uppercase tracking-wide text-slate-500">
                  Instruments
                </dt>
                <dd className="stat-value mt-1">
                  {symbols === null ? '—' : symbols.length}
                </dd>
              </div>
              <div>
                <dt className="text-xs uppercase tracking-wide text-slate-500">
                  Live gate
                </dt>
                <dd className="stat-value mt-1 text-up">Locked</dd>
              </div>
              <div>
                <dt className="text-xs uppercase tracking-wide text-slate-500">
                  Default mode
                </dt>
                <dd className="stat-value mt-1">Paper</dd>
              </div>
              <div>
                <dt className="text-xs uppercase tracking-wide text-slate-500">
                  Credentials
                </dt>
                <dd className="stat-value mt-1 text-up">Encrypted</dd>
              </div>
            </dl>
          </div>
        </section>

        {symbols && symbols.length > 0 ? (
          <section className="border-y border-edge bg-base-900/40">
            <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
              <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">
                Markets available now
              </h2>
              <ul className="mt-4 flex flex-wrap gap-2">
                {symbols.map((s) => (
                  <li
                    key={s.symbol}
                    className="rounded-lg border border-edge bg-base-900/80 px-3 py-1.5 text-sm font-medium text-slate-200"
                  >
                    {s.symbol}
                    {s.asset_class ? (
                      <span className="ml-2 text-xs text-slate-500">{s.asset_class}</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            </div>
          </section>
        ) : null}

        <section className="mx-auto max-w-6xl px-4 py-16 sm:px-6">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {FEATURES.map((f) => (
              <div
                key={f.title}
                className="card transition duration-200 hover:border-brand-500/40 hover:bg-base-900"
              >
                <span className="text-xl text-brand-400" aria-hidden>
                  {f.icon}
                </span>
                <h3 className="mt-3 text-base font-semibold text-slate-50">{f.title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-slate-400">{f.body}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="border-t border-edge bg-base-900/40">
          <div className="mx-auto max-w-3xl px-4 py-16 sm:px-6">
            <h2 className="text-2xl font-semibold tracking-tight text-slate-50">
              What this platform will not tell you
            </h2>
            <p className="mt-2 text-sm text-slate-400">
              Most trading products hide these. They cost nothing to state plainly and
              everything to get wrong.
            </p>
            <ul className="mt-6 space-y-3">
              {GUARDRAILS.map((g) => (
                <li key={g} className="flex gap-3 text-sm leading-relaxed text-slate-300">
                  <span className="mt-0.5 flex-none text-amber-400" aria-hidden>
                    !
                  </span>
                  {g}
                </li>
              ))}
            </ul>
          </div>
        </section>
      </main>

      <footer className="border-t border-edge">
        <div className="mx-auto flex max-w-6xl flex-col gap-2 px-4 py-8 text-xs text-slate-500 sm:flex-row sm:items-center sm:justify-between sm:px-6">
          <p>© {new Date().getFullYear()} Vantage. Trading involves substantial risk of loss.</p>
          <p>Vantage is not a broker and does not provide investment advice.</p>
        </div>
      </footer>
    </div>
  );
}
