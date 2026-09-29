'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { EmptyState, ErrorNote, Pnl, Spinner, Stat } from '@/components/ui';
import { backtestApi, connectEvents } from '@/lib/api';
import type {
  BacktestDetail, BacktestJob, BacktestOptions, BacktestResult, BacktestStatus,
  DrawdownPoint, EquityPointBacktest,
} from '@/lib/types';

/** Fallbacks used only until /backtests/options answers. The server owns this
 *  list; hardcoding it in the client is how you end up offering a strategy
 *  that returns 422. */
const FALLBACK_OPTIONS: BacktestOptions = {
  strategies: [],
  timeframes: ['M1', 'M5', 'M15', 'H1', 'H4'],
  symbols: ['EURUSD'],
  defaults: {
    initial_capital: 10000,
    risk_percent: 1,
    spread_pips: 0.5,
    slippage_pips: 0.2,
    commission_per_lot: 3.5,
  },
};

const STATUS_TONES: Record<BacktestStatus, string> = {
  QUEUED: 'slate', RUNNING: 'warn', COMPLETED: 'up', FAILED: 'down', CANCELLED: 'slate',
};

function StatusBadge({ status }: { status: BacktestStatus }) {
  const tone = STATUS_TONES[status] ?? 'slate';
  return (
    <span
      className={`badge ${
        tone === 'up'
          ? 'bg-up/15 text-up ring-1 ring-inset ring-up/30'
          : tone === 'down'
            ? 'bg-down/15 text-down ring-1 ring-inset ring-down/30'
            : tone === 'warn'
              ? 'bg-amber-500/15 text-amber-400 ring-1 ring-inset ring-amber-500/30'
              : 'bg-slate-500/15 text-slate-300 ring-1 ring-inset ring-slate-500/30'
      }`}
    >
      {status}
    </span>
  );
}

function money(n: number): string {
  return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** Curve points carry ISO timestamps; show only the clock part. */
function shortTime(t: string): string {
  return t.length > 16 ? t.slice(5, 16).replace('T', ' ') : t;
}

function pct(n: number): string {
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;
}

/** Equity and drawdown curves, drawn as plain SVG to match the rest of the app. */
function Curve({
  points, height = 150, baseline, tone, label,
}: {
  points: { t: string; v: number }[];
  height?: number;
  baseline?: number;
  tone: 'up' | 'down';
  label: string;
}) {
  const width = 600;
  if (points.length < 2) {
    return (
      <EmptyState
        title="Not enough data to chart"
        hint="The curve is drawn once the run has produced more than one sample."
      />
    );
  }

  const values = points.map((p) => p.v);
  const candidates = baseline === undefined ? values : [...values, baseline];
  const lo = Math.min(...candidates);
  const hi = Math.max(...candidates);
  const span = hi - lo || 1;
  const px = (i: number) => (i / (points.length - 1)) * width;
  const py = (v: number) => height - ((v - lo) / span) * height;

  const line = values
    .map((v, i) => `${i === 0 ? 'M' : 'L'}${px(i).toFixed(1)},${py(v).toFixed(1)}`)
    .join(' ');
  const area = `${line} L${width},${height} L0,${height} Z`;
  const stroke = tone === 'up' ? 'var(--color-up)' : 'var(--color-down)';

  return (
    <figure className="space-y-2">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full"
        role="img"
        aria-label={label}
      >
        <defs>
          <linearGradient id={`bt-fill-${tone}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity="0.26" />
            <stop offset="100%" stopColor={stroke} stopOpacity="0" />
          </linearGradient>
        </defs>
        {baseline !== undefined && (
          <line
            x1="0"
            x2={width}
            y1={py(baseline)}
            y2={py(baseline)}
            stroke="var(--color-edge)"
            strokeDasharray="4 4"
          />
        )}
        <path d={area} fill={`url(#bt-fill-${tone})`} />
        <path d={line} fill="none" stroke={stroke} strokeWidth="2" strokeLinejoin="round" />
      </svg>
      <figcaption className="flex justify-between text-xs text-slate-500">
        <span>{shortTime(points[0].t)}</span>
        <span>
          min {money(lo)} · max {money(hi)}
        </span>
        <span>{shortTime(points[points.length - 1].t)}</span>
      </figcaption>
    </figure>
  );
}

/** Headline verdict. Reads net P&L first, because a high win rate on a losing
 *  strategy is the single most misleading thing a backtest page can show. */
function Verdict({ r, initialCapital }: { r: BacktestResult; initialCapital: number }) {
  const profitable = r.net_pnl > 0;
  return (
    <div
      className={`rounded-lg border px-4 py-3 ${
        profitable ? 'border-up/40 bg-up/10' : 'border-down/40 bg-down/10'
      }`}
    >
      <p className="text-sm font-semibold text-slate-50">
        {profitable ? 'This configuration made money over the tested period' : 'This configuration lost money over the tested period'}
      </p>
      <p className="mt-1 text-xs text-slate-400">
        Net <Pnl value={r.net_pnl} /> on {money(initialCapital)} starting capital, after{' '}
        {money(r.total_commission)} in commission.
        {r.win_rate >= 50 && !profitable
          ? ` A ${r.win_rate.toFixed(1)}% win rate still lost money: the losses were larger than the wins, and costs made it worse.`
          : ''}
      </p>
    </div>
  );
}

function ResultPanel({ job, detail }: { job: BacktestJob; detail: BacktestDetail | null }) {
  const r = detail?.result ?? null;
  if (!r) {
    if (job.status === 'FAILED') {
      return <ErrorNote message={job.error || 'The backtest failed without reporting a reason.'} />;
    }
    if (job.status === 'RUNNING' || job.status === 'QUEUED') {
      return <Spinner label={`Run ${job.status.toLowerCase()} — ${job.progress.toFixed(0)}%`} />;
    }
    return <EmptyState title="No result recorded" />;
  }

  return (
    <div className="space-y-4">
      <Verdict r={r} initialCapital={job.initial_capital} />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat
          label="Net P/L"
          value={<Pnl value={r.net_pnl} />}
          sub={`ROI ${pct(r.roi_percent)}`}
          tone={r.net_pnl > 0 ? 'up' : r.net_pnl < 0 ? 'down' : 'default'}
        />
        <Stat label="Final equity" value={money(r.final_equity)} sub={`from ${money(job.initial_capital)}`} />
        <Stat
          label="Trades"
          value={r.total_trades}
          sub={`${r.winning_trades} W / ${r.losing_trades} L`}
        />
        <Stat
          label="Win rate"
          value={`${r.win_rate.toFixed(1)}%`}
          sub={
            r.profit_factor === null
              ? 'no profit factor'
              : `profit factor ${r.profit_factor.toFixed(2)}`
          }
        />
        <Stat
          label="Max drawdown"
          value={`${r.max_drawdown_percent.toFixed(2)}%`}
          sub={money(r.max_drawdown_amount)}
          tone={r.max_drawdown_percent > 20 ? 'down' : 'default'}
        />
        <Stat label="Average trade" value={<Pnl value={r.average_trade} />} />
        <Stat label="Best trade" value={<Pnl value={r.best_trade} />} tone="up" />
        <Stat label="Worst trade" value={<Pnl value={r.worst_trade} />} tone="down" />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="card">
          <h3 className="mb-3 text-sm font-semibold text-slate-200">Equity curve</h3>
          <Curve
            points={r.equity_curve.map((p) => ({ t: p.t, v: p.equity }))}
            baseline={job.initial_capital}
            tone={r.net_pnl >= 0 ? 'up' : 'down'}
            label="Account equity over the backtest"
          />
        </div>
        <div className="card">
          <h3 className="mb-3 text-sm font-semibold text-slate-200">Drawdown</h3>
          <Curve
            points={r.drawdown_curve.map((p) => ({ t: p.t, v: p.drawdown_pct }))}
            tone={r.max_drawdown_percent > 20 ? 'down' : 'up'}
            label="Drawdown from peak equity"
          />
        </div>
      </div>

      {r.monthly_returns.length > 0 && (
        <div className="card">
          <h3 className="mb-3 text-sm font-semibold text-slate-200">Monthly returns</h3>
          <div className="flex flex-wrap gap-2">
            {r.monthly_returns.map((m) => (
              <span key={m.month} className="rounded bg-base-900 px-2 py-1 text-xs">
                <span className="text-slate-500">{m.month}</span>{' '}
                <Pnl value={m.return_pct} digits={2} />%
              </span>
            ))}
          </div>
        </div>
      )}

      <details className="card">
        <summary className="cursor-pointer text-sm font-semibold text-slate-200">
          All {r.trades.length} trades
        </summary>
        <div className="mt-3 max-h-96 overflow-auto">
          <table className="w-full text-left text-xs">
            <thead className="sticky top-0 bg-base-900 text-slate-400">
              <tr>
                <th className="py-2 pr-3 font-medium">Side</th>
                <th className="py-2 pr-3 font-medium">Entry</th>
                <th className="py-2 pr-3 font-medium">Exit</th>
                <th className="py-2 pr-3 font-medium">Reason</th>
                <th className="py-2 pr-3 text-right font-medium">Size</th>
                <th className="py-2 pr-3 text-right font-medium">P/L</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-edge">
              {r.trades.map((t, i) => (
                <tr key={i}>
                  <td className="py-1.5 pr-3">
                    <span className={t.side === 'BUY' ? 'text-up' : 'text-down'}>
                      {t.side ?? '—'}
                    </span>
                  </td>
                  <td className="py-1.5 pr-3 tabular text-slate-400">
                    {t.entry_time ? `${shortTime(t.entry_time)} @ ${t.entry_price}` : '—'}
                  </td>
                  <td className="py-1.5 pr-3 tabular text-slate-400">
                    {t.exit_time ? `${shortTime(t.exit_time)} @ ${t.exit_price}` : '—'}
                  </td>
                  <td className="py-1.5 pr-3 text-slate-400">
                    {t.close_reason ? t.close_reason.replace(/_/g, ' ') : '—'}
                  </td>
                  <td className="py-1.5 pr-3 text-right text-slate-400">
                    {t.volume === null ? '—' : `${t.volume} lot`}
                  </td>
                  <td className="py-1.5 text-right">
                    {t.pnl === null ? '—' : <Pnl value={t.pnl} />}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

function NewJobForm({
  options, onQueued,
}: {
  options: BacktestOptions;
  onQueued: (job: BacktestJob) => void;
}) {
  const { defaults } = options;
  const [form, setForm] = useState({
    strategy: options.strategies[0]?.value ?? '',
    symbol: options.symbols[0] ?? '',
    timeframe: options.timeframes[0] ?? 'M1',
    date_from: '',
    date_to: '',
    initial_capital: defaults.initial_capital,
    risk_percent: defaults.risk_percent,
    spread_pips: defaults.spread_pips,
    slippage_pips: defaults.slippage_pips,
    commission_per_lot: defaults.commission_per_lot,
  });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  // Re-seed once, when the real options land and the placeholder guess was
  // wrong (a strategy list that was empty, or a different default capital).
  const [seeded, setSeeded] = useState(false);
  useEffect(() => {
    if (seeded || options.strategies.length === 0) return;
    setForm((f) => ({
      ...f,
      strategy: options.strategies[0]?.value ?? f.strategy,
      symbol: options.symbols[0] ?? f.symbol,
      timeframe: options.timeframes[0] ?? f.timeframe,
      initial_capital: options.defaults.initial_capital,
      risk_percent: options.defaults.risk_percent,
      spread_pips: options.defaults.spread_pips,
      slippage_pips: options.defaults.slippage_pips,
      commission_per_lot: options.defaults.commission_per_lot,
    }));
    setSeeded(true);
  }, [options, seeded]);

  const set = (k: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [k]: k === 'strategy' ? e.target.value : e.target.value }));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const job = await backtestApi.create({
        ...form,
        initial_capital: Number(form.initial_capital),
        risk_percent: Number(form.risk_percent),
        spread_pips: Number(form.spread_pips),
        slippage_pips: Number(form.slippage_pips),
        commission_per_lot: Number(form.commission_per_lot),
      });
      onQueued(job);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not queue the backtest');
    } finally {
      setBusy(false);
    }
  }

  const field = 'w-full rounded-md border border-edge bg-base-900 px-2.5 py-1.5 text-sm text-slate-100 outline-none focus:border-brand-500';
  const label = 'mb-1 block text-xs font-medium text-slate-400';

  return (
    <form onSubmit={submit} className="card space-y-4">
      <div>
        <h2 className="text-sm font-semibold text-slate-100">New backtest</h2>
        <p className="mt-1 text-xs text-slate-500">
          Replays history through the strategy with costs applied. It measures how the rules
          behaved on past data — it does not predict future results.
        </p>
      </div>

      {error ? <ErrorNote message={error} /> : null}

      <div className="grid gap-3 sm:grid-cols-3">
        <div>
          <label className={label} htmlFor="bt-strategy">Strategy</label>
          <select id="bt-strategy" className={field} value={form.strategy} onChange={set('strategy')}>
            {options.strategies.map((s) => (
              <option key={s.value} value={s.value}>{s.label}</option>
            ))}
          </select>
        </div>
        <div>
          <label className={label} htmlFor="bt-symbol">Symbol</label>
          <select id="bt-symbol" className={field} value={form.symbol} onChange={set('symbol')}>
            {options.symbols.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        <div>
          <label className={label} htmlFor="bt-timeframe">Timeframe</label>
          <select id="bt-timeframe" className={field} value={form.timeframe} onChange={set('timeframe')}>
            {options.timeframes.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
        <div>
          <label className={label} htmlFor="bt-from">From</label>
          <input id="bt-from" type="date" required className={field}
                 value={form.date_from} onChange={set('date_from')} />
        </div>
        <div>
          <label className={label} htmlFor="bt-to">To</label>
          <input id="bt-to" type="date" required className={field}
                 value={form.date_to} onChange={set('date_to')} />
        </div>
        <div>
          <label className={label} htmlFor="bt-capital">Starting capital</label>
          <input id="bt-capital" type="number" min={100} step={100} required className={field}
                 value={form.initial_capital} onChange={set('initial_capital')} />
        </div>
        <div>
          <label className={label} htmlFor="bt-risk">Risk per trade %</label>
          <input id="bt-risk" type="number" min={0.1} max={5} step={0.1} required className={field}
                 value={form.risk_percent} onChange={set('risk_percent')} />
        </div>
        <div>
          <label className={label} htmlFor="bt-spread">Spread (pips)</label>
          <input id="bt-spread" type="number" min={0} step={0.1} className={field}
                 value={form.spread_pips} onChange={set('spread_pips')} />
        </div>
        <div>
          <label className={label} htmlFor="bt-commission">Commission / lot</label>
          <input id="bt-commission" type="number" min={0} step={0.1} className={field}
                 value={form.commission_per_lot} onChange={set('commission_per_lot')} />
        </div>
      </div>

      <button
        type="submit"
        disabled={busy}
        className="rounded-md bg-brand-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-brand-400 disabled:opacity-50"
      >
        {busy ? 'Queueing…' : 'Run backtest'}
      </button>
    </form>
  );
}

export default function BacktestsPage() {
  const [jobs, setJobs] = useState<BacktestJob[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<BacktestDetail | null>(null);
  const [error, setError] = useState('');
  const [ready, setReady] = useState(false);
  const [live, setLive] = useState(false);
  const [options, setOptions] = useState<BacktestOptions>(FALLBACK_OPTIONS);
  const busy = useRef<Set<string>>(new Set());

  const load = useCallback(async () => {
    try {
      setJobs(await backtestApi.list());
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load backtests');
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    void backtestApi
      .options()
      .then(setOptions)
      .catch(() => {
        /* keep the fallback; the form still submits and the server validates */
      });
  }, []);

  // Watch for worker progress over the socket, but only refetch a job that is
  // still moving. Finished runs are immutable, so polling them would be waste.
  useEffect(() => {
    if (!live) return;
    return connectEvents((event) => {
      const payload = event.payload as
        | { job_id?: string; status?: BacktestStatus; progress?: number }
        | undefined;
      const jobId = payload?.job_id;
      const status = payload?.status;
      if (!jobId) return;
      setJobs((prev) =>
        prev.map((j) =>
          j.id === jobId && status
            ? { ...j, status, progress: payload?.progress ?? j.progress }
            : j,
        ),
      );
      if (status === 'COMPLETED' || status === 'FAILED') {
        if (busy.current.has(jobId)) return;
        busy.current.add(jobId);
        void backtestApi
          .get(jobId)
          .then((d) => {
            if (selected === jobId) setDetail(d);
          })
          .finally(() => {
            busy.current.delete(jobId);
            void load();
          });
      }
    }, setLive);
  }, [live, load, selected]);

  const open = useCallback(async (id: string) => {
    setSelected(id);
    setDetail(null);
    try {
      setDetail(await backtestApi.get(id));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load backtest');
    }
  }, []);

  async function requeue(job: BacktestJob) {
    try {
      await backtestApi.requeue(job.id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not requeue');
    }
  }

  async function remove(job: BacktestJob) {
    try {
      await backtestApi.remove(job.id);
      if (selected === job.id) { setSelected(null); setDetail(null); }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not delete');
    }
  }

  const current = jobs.find((j) => j.id === selected) ?? null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold text-slate-50">Backtests</h1>
          <p className="mt-0.5 text-sm text-slate-500">
            Replay a strategy over historical bars and see what it would have done.
          </p>
        </div>
        <span className="flex items-center gap-1.5 text-xs text-slate-500">
          <span className={`h-2 w-2 rounded-full ${live ? 'bg-up' : 'bg-slate-600'}`} />
          {live ? 'live updates' : 'realtime offline'}
        </span>
      </div>

      {error ? <ErrorNote message={error} /> : null}

      <NewJobForm
        options={options}
        onQueued={(job) => { void load(); void open(job.id); }}
      />

      {!ready ? (
        <Spinner label="Loading backtests" />
      ) : jobs.length === 0 ? (
        <EmptyState
          title="No backtests yet"
          hint="Pick a strategy and a date range above to run your first replay."
        />
      ) : (
        <div className="card overflow-x-auto p-0">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-edge text-xs text-slate-400">
              <tr>
                <th className="px-4 py-3 font-medium">Strategy</th>
                <th className="px-4 py-3 font-medium">Market</th>
                <th className="px-4 py-3 font-medium">Range</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium">Progress</th>
                <th className="px-4 py-3 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-edge">
              {jobs.map((job) => (
                <tr
                  key={job.id}
                  className={selected === job.id ? 'bg-brand-500/5' : ''}
                >
                  <td className="px-4 py-3">
                    <button
                      onClick={() => void open(job.id)}
                      className="font-medium text-brand-400 hover:underline"
                    >
                      {options.strategies.find((s) => s.value === job.strategy)?.label
                        ?? job.strategy}
                    </button>
                  </td>
                  <td className="px-4 py-3 text-slate-400">
                    {job.symbol} · {job.timeframe}
                  </td>
                  <td className="px-4 py-3 text-xs text-slate-400">
                    {job.date_from} → {job.date_to}
                  </td>
                  <td className="px-4 py-3"><StatusBadge status={job.status} /></td>
                  <td className="px-4 py-3">
                    {job.status === 'RUNNING' ? (
                      <div className="h-1.5 w-24 overflow-hidden rounded-full bg-base-900">
                        <div
                          className="h-full rounded-full bg-brand-500 transition-all"
                          style={{ width: `${job.progress}%` }}
                        />
                      </div>
                    ) : job.status === 'QUEUED' ? (
                      <span className="text-xs text-slate-500">queued</span>
                    ) : (
                      <span className="text-xs text-slate-500">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <div className="flex justify-end gap-2">
                      {(job.status === 'FAILED' || job.status === 'CANCELLED' || job.status === 'COMPLETED') && (
                        <button
                          onClick={() => void requeue(job)}
                          className="text-xs text-brand-400 hover:underline"
                        >
                          Re-run
                        </button>
                      )}
                      <button
                        onClick={() => void remove(job)}
                        className="text-xs text-slate-500 hover:text-down"
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {current && (
        <section className="space-y-3">
          <h2 className="text-sm font-semibold text-slate-200">
            {options.strategies.find((s) => s.value === current.strategy)?.label
              ?? current.strategy}
            <span className="ml-2 font-normal text-slate-500">
              {current.symbol} · {current.timeframe} · {current.date_from} → {current.date_to}
            </span>
          </h2>
          <ResultPanel job={current} detail={detail} />
        </section>
      )}
    </div>
  );
}
