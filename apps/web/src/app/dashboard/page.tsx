'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';

import {
  BotStateBadge, EmptyState, ErrorNote, ModeBadge, Pnl, Spinner, Stat,
} from '@/components/ui';
import { botsApi, connectEvents, dashboardApi, marketApi, tradingApi } from '@/lib/api';
import type { Bot, EquityPoint, PerformanceSummary, Position, Trade } from '@/lib/types';

export default function OverviewPage() {
  const [summary, setSummary] = useState<PerformanceSummary | null>(null);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [bots, setBots] = useState<Bot[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [error, setError] = useState('');
  const [live, setLive] = useState(false);

  const load = useCallback(async () => {
    try {
      const [s, e, b, p, t] = await Promise.all([
        dashboardApi.summary(),
        dashboardApi.equity(30),
        botsApi.list(),
        tradingApi.positions(true),
        tradingApi.trades(),
      ]);
      setSummary(s); setEquity(e); setBots(b); setPositions(p); setTrades(t);
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load dashboard');
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // Live updates: refresh on any engine event, with a light debounce so a
  // burst of order events does not hammer the API.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const dispose = connectEvents(
      () => {
        if (timer) return;
        timer = setTimeout(() => { timer = null; void load(); }, 1500);
      },
      setLive,
    );
    return () => { if (timer) clearTimeout(timer); dispose(); };
  }, [load]);

  if (!summary) return error ? <ErrorNote message={error} /> : <Spinner />;

  const running = bots.filter((b) => b.state === 'RUNNING');

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold text-slate-50">Overview</h1>
        <span className={`text-xs ${live ? 'text-up' : 'text-slate-500'}`}>
          {live ? '● Live' : '○ Reconnecting'}
        </span>
      </div>

      {error ? <ErrorNote message={error} /> : null}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Equity" value={summary.equity.toFixed(2)} sub={`Balance ${summary.balance.toFixed(2)}`} />
        <Stat
          label="Today's P/L"
          value={<Pnl value={summary.realized_today} />}
          sub={`${summary.total_trades} trades all-time`}
        />
        <Stat label="Open positions" value={summary.open_positions} sub={`${running.length} bots running`} />
        <Stat
          label="Win rate"
          value={`${summary.win_rate.toFixed(1)}%`}
          sub={summary.profit_factor ? `PF ${summary.profit_factor.toFixed(2)}` : 'No losses yet'}
        />
      </div>

      <section className="card">
        <h2 className="text-sm font-semibold text-slate-100">Equity (30 days)</h2>
        {equity.length === 0 ? (
          <div className="mt-3">
            <EmptyState
              title="No closed trades yet"
              hint="Your realized P/L will appear here once a trade closes."
            />
          </div>
        ) : (
          <EquityChart points={equity} />
        )}
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="card">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-100">Open positions</h2>
            <Link href="/dashboard/positions" className="text-xs text-brand-400 hover:text-brand-500">
              View all
            </Link>
          </div>
          {positions.length === 0 ? (
            <EmptyState title="No open positions" />
          ) : (
            <ul className="divide-y divide-edge">
              {positions.slice(0, 5).map((p) => (
                <li key={p.id} className="flex items-center justify-between py-2.5 text-sm">
                  <div className="min-w-0">
                    <p className="font-medium text-slate-100">{p.symbol}</p>
                    <p className="text-xs text-slate-500">
                      {p.side} {Number(p.volume).toFixed(2)} @ {Number(p.open_price).toFixed(5)}
                    </p>
                  </div>
                  <Pnl value={p.unrealized_pnl} />
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="card">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-100">Recent trades</h2>
            <Link href="/dashboard/trades" className="text-xs text-brand-400 hover:text-brand-500">
              View all
            </Link>
          </div>
          {trades.length === 0 ? (
            <EmptyState title="No trades yet" />
          ) : (
            <ul className="divide-y divide-edge">
              {trades.slice(0, 5).map((t) => (
                <li key={t.id} className="flex items-center justify-between py-2.5 text-sm">
                  <div className="min-w-0">
                    <p className="font-medium text-slate-100">{t.symbol}</p>
                    <p className="text-xs text-slate-500">
                      {new Date(t.closed_at).toLocaleString()}
                    </p>
                  </div>
                  <Pnl value={t.pnl} />
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      <section className="card">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-100">Bots</h2>
          <Link href="/dashboard/bots" className="text-xs text-brand-400 hover:text-brand-500">
            Manage
          </Link>
        </div>
        {bots.length === 0 ? (
          <EmptyState
            title="No bots configured"
            hint="Create a bot to start paper trading a strategy."
          />
        ) : (
          <ul className="divide-y divide-edge">
            {bots.slice(0, 5).map((b) => (
              <li key={b.id} className="flex items-center justify-between gap-3 py-2.5">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-slate-100">{b.name}</p>
                  <p className="text-xs text-slate-500">{b.symbol} · {b.strategy}</p>
                </div>
                <div className="flex flex-none items-center gap-1.5">
                  <ModeBadge mode={b.mode} />
                  <BotStateBadge state={b.state} />
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

/** Dependency-free SVG line chart: scales to the container width. */
function EquityChart({ points }: { points: EquityPoint[] }) {
  const w = 600;
  const h = 160;
  const pad = 8;

  const values = points.map((p) => p.equity);
  const min = Math.min(...values, 0);
  const max = Math.max(...values, 0);
  const span = max - min || 1;

  const xy = points.map((p, i) => {
    const x = pad + (i / Math.max(points.length - 1, 1)) * (w - pad * 2);
    const y = h - pad - ((p.equity - min) / span) * (h - pad * 2);
    return [x, y] as const;
  });

  const line = xy.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const area = `${line} L${xy[xy.length - 1][0].toFixed(1)},${h - pad} L${xy[0][0].toFixed(1)},${h - pad} Z`;
  const last = values[values.length - 1];
  const positive = last >= 0;
  const stroke = positive ? '#22c55e' : '#ef4444';

  return (
    <div className="mt-3">
      <svg
        viewBox={`0 0 ${w} ${h}`}
        className="h-40 w-full"
        preserveAspectRatio="none"
        role="img"
        aria-label={`Equity curve, final value ${last.toFixed(2)}`}
      >
        <defs>
          <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity="0.25" />
            <stop offset="100%" stopColor={stroke} stopOpacity="0" />
          </linearGradient>
        </defs>
        <path d={area} fill="url(#eq)" />
        <path d={line} fill="none" stroke={stroke} strokeWidth="2" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="mt-1 flex justify-between text-[11px] text-slate-500">
        <span>{new Date(points[0].t).toLocaleDateString()}</span>
        <span className={positive ? 'text-up' : 'text-down'}>
          {positive ? '+' : ''}{last.toFixed(2)}
        </span>
        <span>{new Date(points[points.length - 1].t).toLocaleDateString()}</span>
      </div>
    </div>
  );
}
