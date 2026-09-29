'use client';

import { useCallback, useEffect, useState, type FormEvent } from 'react';

import { BotStateBadge, EmptyState, ErrorNote, ModeBadge, Spinner } from '@/components/ui';
import { botsApi, marketApi } from '@/lib/api';
import type { Bot, SymbolInfo } from '@/lib/types';

const STRATEGIES = [
  { value: 'hft_scalper', label: 'HFT Scalper' },
  { value: 'mean_reversion', label: 'Mean Reversion' },
  { value: 'trend_follower', label: 'Trend Follower' },
];

export default function BotsPage() {
  const [bots, setBots] = useState<Bot[]>([]);
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [error, setError] = useState('');
  const [busyId, setBusyId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [ready, setReady] = useState(false);

  const load = useCallback(async () => {
    try {
      setBots(await botsApi.list());
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load bots');
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => {
    void load();
    marketApi.symbols().then(setSymbols).catch(() => setSymbols([]));
  }, [load]);

  async function act(bot: Bot, action: 'start' | 'stop' | 'pause' | 'resume' | 'killSwitch') {
    setBusyId(bot.id);
    setError('');
    try {
      const fn = { start: botsApi.start, stop: botsApi.stop, pause: botsApi.pause,
        resume: botsApi.resume, killSwitch: botsApi.killSwitch }[action];
      await fn(bot.id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action failed');
    } finally {
      setBusyId(null);
    }
  }

  async function remove(bot: Bot) {
    if (!confirm(`Delete "${bot.name}"? This cannot be undone.`)) return;
    setBusyId(bot.id);
    try {
      await botsApi.remove(bot.id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Delete failed');
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold text-slate-50">Bots</h1>
        <button className="btn-primary" onClick={() => setShowForm((s) => !s)}>
          {showForm ? 'Cancel' : 'New bot'}
        </button>
      </div>

      {error ? <ErrorNote message={error} /> : null}

      {showForm ? (
        <NewBotForm
          symbols={symbols}
          onCreated={async () => { setShowForm(false); await load(); }}
        />
      ) : null}

      {!ready ? (
        <Spinner />
      ) : bots.length === 0 ? (
        <EmptyState
          title="No bots yet"
          hint="Create a bot in PAPER mode to test a strategy with simulated fills."
        />
      ) : (
        <ul className="space-y-3">
          {bots.map((bot) => (
            <li key={bot.id} className="card">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="truncate font-medium text-slate-100">{bot.name}</h2>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {bot.symbol} · {bot.strategy} · {bot.timeframe}
                  </p>
                </div>
                <div className="flex flex-none items-center gap-1.5">
                  <ModeBadge mode={bot.mode} />
                  <BotStateBadge state={bot.state} />
                </div>
              </div>

              <div className="mt-4 flex flex-wrap gap-2">
                {bot.state === 'RUNNING' ? (
                  <>
                    <button className="btn-ghost" disabled={busyId === bot.id}
                      onClick={() => act(bot, 'pause')}>Pause</button>
                    <button className="btn-ghost" disabled={busyId === bot.id}
                      onClick={() => act(bot, 'stop')}>Stop</button>
                  </>
                ) : bot.state === 'PAUSED' ? (
                  <>
                    <button className="btn-success" disabled={busyId === bot.id}
                      onClick={() => act(bot, 'resume')}>Resume</button>
                    <button className="btn-ghost" disabled={busyId === bot.id}
                      onClick={() => act(bot, 'stop')}>Stop</button>
                  </>
                ) : (
                  <button className="btn-primary" disabled={busyId === bot.id}
                    onClick={() => act(bot, 'start')}>Start</button>
                )}

                {bot.state === 'RUNNING' && (
                  <button className="btn-danger" disabled={busyId === bot.id}
                    onClick={() => {
                      if (confirm('Emergency stop? Trading halts immediately and open positions are flattened.'))
                        act(bot, 'killSwitch');
                    }}>
                    Kill switch
                  </button>
                )}

                <button className="btn-ghost ml-auto" disabled={busyId === bot.id}
                  onClick={() => remove(bot)}>Delete</button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function NewBotForm({
  symbols, onCreated,
}: { symbols: SymbolInfo[]; onCreated: () => Promise<void> }) {
  const [form, setForm] = useState({
    name: '', symbol: 'EURUSD', strategy: 'hft_scalper', timeframe: 'M1',
  });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const set = (k: keyof typeof form, v: string) => setForm((f) => ({ ...f, [k]: v }));

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      // Bots are always created in PAPER mode: the API rejects LIVE creation,
      // and live rights are a separate, explicit grant.
      await botsApi.create({ ...form, mode: 'PAPER', auto_start: false });
      await onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create bot');
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="card space-y-4">
      <h2 className="text-sm font-semibold text-slate-100">New paper-trading bot</h2>
      {error ? <ErrorNote message={error} /> : null}
      <div>
        <label className="label" htmlFor="bot-name">Name</label>
        <input id="bot-name" required maxLength={80} value={form.name}
          onChange={(e) => set('name', e.target.value)} placeholder="EURUSD morning scalper" />
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
        <div>
          <label className="label" htmlFor="bot-symbol">Symbol</label>
          <select id="bot-symbol" value={form.symbol} onChange={(e) => set('symbol', e.target.value)}>
            {symbols.length === 0 ? <option value="EURUSD">EURUSD</option> : null}
            {symbols.map((s) => <option key={s.symbol} value={s.symbol}>{s.symbol}</option>)}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="bot-strategy">Strategy</label>
          <select id="bot-strategy" value={form.strategy}
            onChange={(e) => set('strategy', e.target.value)}>
            {STRATEGIES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="bot-tf">Timeframe</label>
          <select id="bot-tf" value={form.timeframe} onChange={(e) => set('timeframe', e.target.value)}>
            {['M1', 'M5', 'M15', 'M30', 'H1', 'H4'].map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
      </div>
      <p className="text-xs text-slate-500">
        New bots start in PAPER mode with simulated fills. You can request live
        permission later; nothing trades real money without it.
      </p>
      <button type="submit" className="btn-primary" disabled={busy}>
        {busy ? 'Creating…' : 'Create bot'}
      </button>
    </form>
  );
}
