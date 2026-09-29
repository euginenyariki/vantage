'use client';

import { useCallback, useEffect, useState } from 'react';

import { analyticsApi } from '@/lib/api';
import type { AnalyticsReport } from '@/lib/types';
import { EmptyState, ErrorNote, Pnl, Spinner, Stat } from '@/components/ui';
import { useAuth } from '@/lib/auth';

const RANGES = [7, 30, 90, 365] as const;

/**
 * Renders a metric that may be unavailable.
 *
 * A null metric means the data cannot support the number (no losing trades, no
 * variance to measure), which is different from a real zero. Showing "0.00" or
 * "1.00" there would quietly claim something the account has not earned, so we
 * say so instead.
 */
function Maybe({ value, suffix = '', digits = 2 }: { value: number | null; suffix?: string; digits?: number }) {
  if (value === null || !Number.isFinite(value)) {
    return <span className="text-slate-500">n/a</span>;
  }
  return (
    <span className="tabular">
      {value.toFixed(digits)}
      {suffix}
    </span>
  );
}

function equityPath(
  points: AnalyticsReport['equity'],
  width: number,
  height: number,
): { line: string; area: string; zeroY: number } | null {
  if (points.length < 2) return null;
  const values = points.map((p) => p.cumulative_pnl);
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const span = max - min || 1;
  const x = (i: number) => (i / (points.length - 1)) * width;
  const y = (v: number) => height - ((v - min) / span) * height;
  const zeroY = y(0);

  const line = values.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const area = `${line} L${width},${zeroY.toFixed(1)} L0,${zeroY.toFixed(1)} Z`;
  return { line, area, zeroY };
}

function EquityChart({ points }: { points: AnalyticsReport['equity'] }) {
  const width = 720;
  const height = 200;
  const geometry = equityPath(points, width, height);

  if (!geometry) {
    return (
      <EmptyState
        title="Not enough closed trades to chart"
        hint="The equity curve needs at least two days of closed trades before it can be drawn."
      />
    );
  }

  const { line, area, zeroY } = geometry;
  const positive = points[points.length - 1].cumulative_pnl >= 0;
  const stroke = positive ? 'var(--color-up)' : 'var(--color-down)';

  return (
    <figure className="space-y-2">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full"
        role="img"
        aria-label="Cumulative profit and loss over time"
      >
        <defs>
          <linearGradient id="equity-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity="0.28" />
            <stop offset="100%" stopColor={stroke} stopOpacity="0" />
          </linearGradient>
        </defs>
        <line
          x1="0"
          y1={zeroY}
          x2={width}
          y2={zeroY}
          stroke="var(--color-edge)"
          strokeDasharray="4 4"
        />
        <path d={area} fill="url(#equity-fill)" />
        <path d={line} fill="none" stroke={stroke} strokeWidth="2" strokeLinejoin="round" />
      </svg>
      <figcaption className="flex justify-between text-xs text-slate-500">
        <span>{points[0].date ?? '—'}</span>
        <span>
          Cumulative P/L <Pnl value={points[points.length - 1].cumulative_pnl} />
        </span>
        <span>{points[points.length - 1].date ?? '—'}</span>
      </figcaption>
    </figure>
  );
}

/** Both breakdown shapes share the same performance columns. */
type BreakdownRow = {
  trades: number;
  win_rate: number;
  net_pnl: number;
  expectancy: number;
  profit_factor: number | null;
};

function BreakdownTable<K extends 'symbol' | 'strategy'>({
  rows,
  labelKey,
  labelHeader,
}: {
  rows: ({ [P in K]: string } & BreakdownRow)[];
  labelKey: K;
  labelHeader: string;
}) {
  if (!rows.length) {
    return <EmptyState title="No closed trades in this period" />;
  }
  return (
    <div className="card overflow-x-auto p-0">
      <table className="w-full text-sm">
        <thead className="border-b border-edge text-xs uppercase tracking-wide text-slate-400">
          <tr>
            <th className="px-4 py-2.5 text-left font-medium">{labelHeader}</th>
            <th className="px-4 py-2.5 text-right font-medium">Trades</th>
            <th className="px-4 py-2.5 text-right font-medium">Win rate</th>
            <th className="px-4 py-2.5 text-right font-medium">Net P/L</th>
            <th className="px-4 py-2.5 text-right font-medium">Expectancy</th>
            <th className="px-4 py-2.5 text-right font-medium">Profit factor</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-edge">
          {rows.map((row) => (
            <tr key={row[labelKey]}>
              <td className="px-4 py-2.5 font-medium text-slate-100">{row[labelKey]}</td>
              <td className="tabular px-4 py-2.5 text-right text-slate-300">{row.trades}</td>
              <td className="tabular px-4 py-2.5 text-right text-slate-300">
                {row.win_rate.toFixed(1)}%
              </td>
              <td className="px-4 py-2.5 text-right">
                <Pnl value={row.net_pnl} />
              </td>
              <td className="tabular px-4 py-2.5 text-right text-slate-300">
                <Pnl value={row.expectancy} digits={3} />
              </td>
              <td className="tabular px-4 py-2.5 text-right text-slate-300">
                <Maybe value={row.profit_factor} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function AnalyticsPage() {
  const { ready } = useAuth();
  const [days, setDays] = useState<number>(90);
  const [report, setReport] = useState<AnalyticsReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setReport(await analyticsApi.report(days));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load analytics');
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    if (!ready) return;
    void load();
  }, [load, ready]);

  const m = report?.metrics;

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-50">Performance</h1>
          <p className="mt-1 text-sm text-slate-400">
            Computed from your closed trades. Every figure below is measured, not modelled.
          </p>
        </div>
        <div className="flex gap-1 rounded-lg border border-edge bg-base-900 p-1">
          {RANGES.map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => setDays(r)}
              aria-pressed={days === r}
              className={`rounded-md px-3 py-1.5 text-sm font-medium transition ${
                days === r
                  ? 'bg-brand-500/15 text-brand-400'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              {r}d
            </button>
          ))}
        </div>
      </header>

      {error ? <ErrorNote message={error} /> : null}
      {loading ? <Spinner label="Crunching your trade history" /> : null}

      {!loading && !error && m ? (
        <>
          {m.total_trades === 0 ? (
            <EmptyState
              title="No closed trades in this window"
              hint="Once a bot closes its first trade, expectancy, profit factor, drawdown and streaks will appear here."
            />
          ) : null}

          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <Stat
              label="Net P/L"
              value={<Pnl value={m.net_pnl} />}
              sub={`${m.total_trades} closed trades`}
              tone={m.net_pnl > 0 ? 'up' : m.net_pnl < 0 ? 'down' : 'default'}
            />
            <Stat
              label="Win rate"
              value={`${m.win_rate.toFixed(1)}%`}
              sub={`${m.winning_trades}W / ${m.losing_trades}L`}
            />
            {/* Expectancy is the number that decides whether a strategy is worth
                running, so it leads rather than the flattering win rate. */}
            <Stat
              label="Expectancy / trade"
              value={<Pnl value={m.expectancy} digits={3} />}
              sub="average result per trade"
              tone={m.expectancy > 0 ? 'up' : m.expectancy < 0 ? 'down' : 'default'}
            />
            <Stat
              label="Profit factor"
              value={<Maybe value={m.profit_factor} />}
              sub={
                m.profit_factor === null
                  ? 'no losing trades yet'
                  : m.profit_factor > 1
                    ? 'gross profit above gross loss'
                    : 'gross loss exceeds gross profit'
              }
            />
          </div>

          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <Stat
              label="Max drawdown"
              value={m.max_drawdown.toFixed(2)}
              sub={m.max_drawdown_percent > 0 ? `${m.max_drawdown_percent.toFixed(1)}% from peak` : 'peak-to-trough'}
              tone={m.max_drawdown > 0 ? 'down' : 'default'}
            />
            <Stat
              label="Payoff ratio"
              value={<Maybe value={m.payoff_ratio} />}
              sub={
                m.payoff_ratio === null
                  ? 'no losing trades yet'
                  : 'avg win vs avg loss'
              }
            />
            <Stat
              label="Sharpe (P/L based)"
              value={<Maybe value={m.sharpe_pnl_ratio} digits={2} />}
              sub="from daily P/L, not equity"
            />
            <Stat
              label="Longest streak"
              value={`${m.longest_win_streak}W / ${m.longest_loss_streak}L`}
              sub="consecutive trades"
            />
          </div>

          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <Stat label="Gross profit" value={m.gross_profit.toFixed(2)} tone="up" sub={`avg win ${m.avg_win.toFixed(2)}`} />
            <Stat label="Gross loss" value={m.gross_loss.toFixed(2)} tone="down" sub={`avg loss ${m.avg_loss.toFixed(2)}`} />
            <Stat label="Largest win" value={m.largest_win.toFixed(2)} tone="up" />
            <Stat label="Largest loss" value={m.largest_loss.toFixed(2)} tone="down" />
          </div>

          <section className="space-y-3">
            <h2 className="text-lg font-semibold text-slate-100">Equity curve</h2>
            <div className="card">
              <EquityChart points={report!.equity} />
            </div>
            <p className="text-xs text-slate-500">
              Sharpe is annualised from daily profit and loss rather than a percentage
              return on equity, because paper accounts without a linked broker have no
              daily balance series. Treat it as a P/L consistency measure, not a
              return-on-capital figure.
            </p>
          </section>

          <section className="space-y-3">
            <h2 className="text-lg font-semibold text-slate-100">By market</h2>
            <BreakdownTable rows={report!.by_symbol} labelKey="symbol" labelHeader="Symbol" />
          </section>

          <section className="space-y-3">
            <h2 className="text-lg font-semibold text-slate-100">By strategy</h2>
            <BreakdownTable
              rows={report!.by_strategy}
              labelKey="strategy"
              labelHeader="Strategy"
            />
          </section>
        </>
      ) : null}
    </div>
  );
}
