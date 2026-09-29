'use client';

import type { ReactNode } from 'react';

import type { BotState, TradingMode } from '@/lib/types';

export function Badge({ children, tone = 'slate' }: { children: ReactNode; tone?: string }) {
  const tones: Record<string, string> = {
    slate: 'bg-slate-500/15 text-slate-300 ring-1 ring-inset ring-slate-500/30',
    up: 'bg-up/15 text-up ring-1 ring-inset ring-up/30',
    down: 'bg-down/15 text-down ring-1 ring-inset ring-down/30',
    brand: 'bg-brand-500/15 text-brand-400 ring-1 ring-inset ring-brand-500/30',
    warn: 'bg-amber-500/15 text-amber-400 ring-1 ring-inset ring-amber-500/30',
  };
  return <span className={`badge ${tones[tone] ?? tones.slate}`}>{children}</span>;
}

const BOT_TONES: Record<BotState, string> = {
  RUNNING: 'up',
  STARTING: 'warn',
  STOPPING: 'warn',
  PAUSING: 'warn',
  PAUSED: 'warn',
  ERROR: 'down',
  EMERGENCY_STOP: 'down',
  OFFLINE: 'slate',
  STOPPED: 'slate',
};

const MODE_TONES: Record<TradingMode, string> = {
  LIVE: 'down',
  DEMO: 'warn',
  PAPER: 'brand',
};

export function BotStateBadge({ state }: { state: BotState }) {
  return <Badge tone={BOT_TONES[state] ?? 'slate'}>{state.replace('_', ' ')}</Badge>;
}

export function ModeBadge({ mode }: { mode: TradingMode }) {
  return <Badge tone={MODE_TONES[mode] ?? 'slate'}>{mode}</Badge>;
}

export function Pnl({ value, digits = 2 }: { value: number | string; digits?: number }) {
  const n = typeof value === 'string' ? parseFloat(value) : value;
  if (!Number.isFinite(n)) return <span className="tabular text-slate-500">—</span>;
  const tone = n > 0 ? 'text-up' : n < 0 ? 'text-down' : 'text-slate-400';
  return (
    <span className={`tabular font-medium ${tone}`}>
      {n > 0 ? '+' : ''}
      {n.toFixed(digits)}
    </span>
  );
}

/** Compact stat tile used across the dashboard. */
export function Stat({
  label, value, sub, tone = 'default',
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: 'default' | 'up' | 'down';
}) {
  const valueTone =
    tone === 'up' ? 'text-up' : tone === 'down' ? 'text-down' : 'text-slate-50';
  return (
    <div className="card">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-400">{label}</p>
      <p className={`stat-value mt-1.5 ${valueTone}`}>{value}</p>
      {sub ? <p className="mt-1 text-xs text-slate-500">{sub}</p> : null}
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed border-edge px-4 py-10 text-center">
      <p className="text-sm font-medium text-slate-300">{title}</p>
      {hint ? <p className="max-w-sm text-xs text-slate-500">{hint}</p> : null}
    </div>
  );
}

export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-10 text-sm text-slate-400">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-edge border-t-brand-400" />
      {label}
    </div>
  );
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-down/40 bg-down/10 px-3 py-2.5 text-sm text-down"
    >
      {message}
    </div>
  );
}
