'use client';

import { useCallback, useEffect, useState, type FormEvent } from 'react';

import { Badge, EmptyState, ErrorNote, Spinner } from '@/components/ui';
import { brokersApi } from '@/lib/api';
import type { BrokerConnection, BrokerConnectionInput, ConnectionTestResult } from '@/lib/types';

const EMPTY: BrokerConnectionInput = {
  name: '', broker: 'metaapi', is_demo: true,
  login: '', password: '', server: '', currency: 'USD',
};

/** Connection state -> badge tone. */
function stateTone(state: string): string {
  if (state === 'CONNECTED') return 'emerald';
  if (state === 'ERROR') return 'red';
  if (state === 'CONNECTING') return 'amber';
  return 'slate';
}

export default function BrokersPage() {
  const [connections, setConnections] = useState<BrokerConnection[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [form, setForm] = useState<BrokerConnectionInput>(EMPTY);
  const [showForm, setShowForm] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [result, setResult] = useState<ConnectionTestResult | null>(null);
  const [ready, setReady] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      setConnections(await brokersApi.list());
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load connections');
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  function set<K extends keyof BrokerConnectionInput>(k: K, v: BrokerConnectionInput[K]) {
    setForm((f) => ({ ...f, [k]: v }));
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError('');
    setResult(null);
    try {
      await brokersApi.create(form);
      setForm(EMPTY);
      setShowForm(false);
      setNotice('Connection saved. Credentials are encrypted at rest.');
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save the connection');
    } finally {
      setSaving(false);
    }
  }

  async function test(conn: BrokerConnection) {
    setTestingId(conn.id);
    setError('');
    setResult(null);
    setNotice('');
    try {
      const r = await brokersApi.test(conn.id);
      setResult(r);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Connection test failed');
    } finally {
      setTestingId(null);
    }
  }

  async function disconnect(conn: BrokerConnection) {
    setBusyId(conn.id);
    setError('');
    try {
      await brokersApi.disconnect(conn.id);
      setNotice(`Disconnected "${conn.name}".`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Disconnect failed');
    } finally {
      setBusyId(null);
    }
  }

  async function remove(conn: BrokerConnection) {
    if (!confirm(`Delete "${conn.name}"? The stored credentials are removed permanently.`)) return;
    setBusyId(conn.id);
    setError('');
    try {
      await brokersApi.remove(conn.id);
      setNotice(`Deleted "${conn.name}".`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Delete failed');
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">Broker connections</h1>
          <p className="mt-1 text-sm text-slate-400">
            Link an MT4 or MT5 account. Logins and passwords are encrypted before
            they are stored and are never shown again.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowForm((v) => !v)}
          className="rounded-lg bg-brand-500 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-600"
        >
          {showForm ? 'Cancel' : 'Add connection'}
        </button>
      </header>

      <ErrorNote message={error} />
      {notice && (
        <p className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300">
          {notice}
        </p>
      )}
      {result && (
        <div
          className={`rounded-lg border px-3 py-2 text-sm ${
            result.ok
              ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
              : 'border-red-500/30 bg-red-500/10 text-red-300'
          }`}
        >
          <strong>{result.ok ? 'Connection succeeded' : 'Connection failed'}</strong>
          {' — '}
          {result.message}
          {result.latency_ms != null && ` (${result.latency_ms} ms)`}
          {result.balance != null && ` · balance ${result.balance} ${result.currency ?? ''}`}
        </div>
      )}

      {showForm && (
        <form
          onSubmit={submit}
          className="space-y-4 rounded-xl border border-slate-800 bg-base-900 p-4 sm:p-5"
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block text-sm">
              <span className="mb-1 block text-slate-300">Connection name</span>
              <input
                required
                value={form.name}
                onChange={(e) => set('name', e.target.value)}
                placeholder="MT5 Demo"
                className="w-full rounded-lg border border-slate-700 bg-base-850 px-3 py-2.5 text-base text-slate-100 sm:text-sm"
              />
            </label>
            <label className="block text-sm">
              <span className="mb-1 block text-slate-300">Broker</span>
              <select
                value={form.broker}
                onChange={(e) => set('broker', e.target.value)}
                className="w-full rounded-lg border border-slate-700 bg-base-850 px-3 py-2.5 text-base text-slate-100 sm:text-sm"
              >
                <option value="metaapi">MetaApi (MT4 / MT5)</option>
              </select>
            </label>
            <label className="block text-sm">
              <span className="mb-1 block text-slate-300">Account login</span>
              <input
                required
                inputMode="numeric"
                autoComplete="off"
                value={form.login}
                onChange={(e) => set('login', e.target.value)}
                placeholder="1234567"
                className="w-full rounded-lg border border-slate-700 bg-base-850 px-3 py-2.5 text-base text-slate-100 sm:text-sm"
              />
            </label>
            <label className="block text-sm">
              <span className="mb-1 block text-slate-300">Password</span>
              <input
                required
                type="password"
                autoComplete="new-password"
                value={form.password}
                onChange={(e) => set('password', e.target.value)}
                className="w-full rounded-lg border border-slate-700 bg-base-850 px-3 py-2.5 text-base text-slate-100 sm:text-sm"
              />
            </label>
            <label className="block text-sm">
              <span className="mb-1 block text-slate-300">Server</span>
              <input
                required
                value={form.server}
                onChange={(e) => set('server', e.target.value)}
                placeholder="MetaQuotes-Demo"
                className="w-full rounded-lg border border-slate-700 bg-base-850 px-3 py-2.5 text-base text-slate-100 sm:text-sm"
              />
            </label>
            <label className="block text-sm">
              <span className="mb-1 block text-slate-300">Currency</span>
              <input
                value={form.currency}
                onChange={(e) => set('currency', e.target.value.toUpperCase())}
                placeholder="USD"
                className="w-full rounded-lg border border-slate-700 bg-base-850 px-3 py-2.5 text-base text-slate-100 sm:text-sm"
              />
            </label>
          </div>

          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={form.is_demo}
              onChange={(e) => set('is_demo', e.target.checked)}
              className="h-4 w-4 accent-brand-500"
            />
            This is a demo account (recommended)
          </label>

          <button
            type="submit"
            disabled={saving}
            className="w-full rounded-lg bg-brand-500 px-4 py-2.5 text-sm font-semibold text-white hover:bg-brand-600 disabled:opacity-60 sm:w-auto"
          >
            {saving ? 'Saving…' : 'Save connection'}
          </button>
        </form>
      )}

      {!ready ? (
        <Spinner label="Loading connections" />
      ) : connections.length === 0 ? (
        <EmptyState
          title="No broker connections yet"
          hint="Add an MT4 or MT5 account to let bots place real orders."
        />
      ) : (
        <ul className="grid gap-3 sm:grid-cols-2">
          {connections.map((c) => (
            <li
              key={c.id}
              className="rounded-xl border border-slate-800 bg-base-900 p-4"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate font-medium text-slate-100">{c.name}</p>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {c.broker} · {c.masked_login} · {c.currency}
                  </p>
                </div>
                <div className="flex shrink-0 flex-col items-end gap-1">
                  <Badge tone={c.is_demo ? 'sky' : 'amber'}>
                    {c.is_demo ? 'Demo' : 'Live'}
                  </Badge>
                  <Badge tone={stateTone(c.connection_state)}>{c.connection_state}</Badge>
                </div>
              </div>

              {c.balance != null && (
                <p className="mt-3 text-sm text-slate-300">
                  Balance {c.balance} {c.currency}
                  {c.equity != null && ` · Equity ${c.equity}`}
                </p>
              )}
              {c.last_error && (
                <p className="mt-2 break-words text-xs text-red-400">{c.last_error}</p>
              )}

              <div className="mt-4 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => test(c)}
                  disabled={testingId === c.id}
                  className="min-h-[44px] flex-1 rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-200 hover:bg-base-850 disabled:opacity-60"
                >
                  {testingId === c.id ? 'Testing…' : 'Test connection'}
                </button>
                <button
                  type="button"
                  onClick={() => disconnect(c)}
                  disabled={busyId === c.id}
                  className="min-h-[44px] rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-base-850 disabled:opacity-60"
                >
                  Disconnect
                </button>
                <button
                  type="button"
                  onClick={() => remove(c)}
                  disabled={busyId === c.id}
                  className="min-h-[44px] rounded-lg border border-red-500/40 px-3 py-2 text-sm text-red-300 hover:bg-red-500/10 disabled:opacity-60"
                >
                  Delete
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
