'use client';

import { useCallback, useEffect, useState } from 'react';

import { adminApi, api } from '@/lib/api';
import type { AdminBot, AdminBroker, AdminUser, PlatformStats } from '@/lib/types';
import { ErrorNote, Spinner, Stat } from '@/components/ui';
import { useAuth } from '@/lib/auth';

type Tab = 'users' | 'brokers' | 'bots';

const TABS: { id: Tab; label: string }[] = [
  { id: 'users', label: 'Users' },
  { id: 'brokers', label: 'Brokers' },
  { id: 'bots', label: 'Bots' },
];

function formatDate(value: string | null): string {
  if (!value) return '—';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString();
}

function UsersTab({ selfId }: { selfId: string }) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await adminApi.users(query || undefined);
      setUsers(res.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load users');
    }
  }, [query]);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = async (u: AdminUser) => {
    setBusy(u.id);
    setError(null);
    try {
      await adminApi.setUserActive(u.id, !u.is_active);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Update failed');
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-3">
      <input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search by name or email"
        className="w-full max-w-sm rounded-lg border border-edge bg-base-900 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 sm:w-80"
        aria-label="Search users"
      />
      {error ? <ErrorNote message={error} /> : null}
      <div className="card overflow-x-auto p-0">
        <table className="w-full text-sm">
          <thead className="border-b border-edge text-xs uppercase tracking-wide text-slate-400">
            <tr>
              <th className="px-4 py-2.5 text-left font-medium">User</th>
              <th className="px-4 py-2.5 text-left font-medium">Role</th>
              <th className="px-4 py-2.5 text-right font-medium">Bots</th>
              <th className="px-4 py-2.5 text-right font-medium">Brokers</th>
              <th className="px-4 py-2.5 text-left font-medium">Last login</th>
              <th className="px-4 py-2.5 text-right font-medium">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-edge">
            {users.map((u) => {
              const isSelf = u.id === selfId;
              return (
                <tr key={u.id}>
                  <td className="px-4 py-2.5">
                    <p className="font-medium text-slate-100">{u.name}</p>
                    <p className="text-xs text-slate-500">{u.email}</p>
                  </td>
                  <td className="px-4 py-2.5">
                    <span
                      className={
                        u.role === 'USER'
                          ? 'text-slate-400'
                          : 'rounded bg-brand-500/15 px-1.5 py-0.5 text-xs text-brand-400'
                      }
                    >
                      {u.role.replace('_', ' ')}
                    </span>
                  </td>
                  <td className="tabular px-4 py-2.5 text-right text-slate-300">{u.bots}</td>
                  <td className="tabular px-4 py-2.5 text-right text-slate-300">
                    {u.broker_connections}
                  </td>
                  <td className="px-4 py-2.5 text-slate-400">{formatDate(u.last_login_at)}</td>
                  <td className="px-4 py-2.5 text-right">
                    <div className="flex items-center justify-end gap-2">
                      <span
                        className={
                          u.is_active ? 'text-up' : 'text-slate-500'
                        }
                      >
                        {u.is_active ? 'active' : 'disabled'}
                      </span>
                      <button
                        type="button"
                        onClick={() => void toggle(u)}
                        disabled={busy === u.id || isSelf}
                        title={
                          isSelf
                            ? 'You cannot disable your own account'
                            : undefined
                        }
                        className="rounded-md border border-edge px-2 py-1 text-xs font-medium text-slate-200 transition hover:border-brand-500/50 hover:text-brand-400 disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        {u.is_active ? 'Disable' : 'Enable'}
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function BrokersTab({ brokers }: { brokers: AdminBroker[] }) {
  if (!brokers.length) {
    return (
      <p className="card text-sm text-slate-400">
        No broker connections on the platform yet.
      </p>
    );
  }
  return (
    <div className="card overflow-x-auto p-0">
      <table className="w-full text-sm">
        <thead className="border-b border-edge text-xs uppercase tracking-wide text-slate-400">
          <tr>
            <th className="px-4 py-2.5 text-left font-medium">Connection</th>
            <th className="px-4 py-2.5 text-left font-medium">Owner</th>
            <th className="px-4 py-2.5 text-left font-medium">State</th>
            <th className="px-4 py-2.5 text-right font-medium">Balance</th>
            <th className="px-4 py-2.5 text-left font-medium">Last sync</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-edge">
          {brokers.map((b) => (
            <tr key={b.id}>
              <td className="px-4 py-2.5">
                <p className="font-medium text-slate-100">{b.name}</p>
                <p className="text-xs text-slate-500">
                  {b.broker} · {b.is_demo ? 'demo' : 'live'}
                </p>
              </td>
              <td className="px-4 py-2.5 text-slate-300">{b.user_email}</td>
              <td className="px-4 py-2.5">
                <span className={b.connection_state === 'CONNECTED' ? 'text-up' : 'text-slate-400'}>
                  {b.connection_state}
                </span>
                {b.last_error ? (
                  <p className="mt-0.5 max-w-xs truncate text-xs text-down" title={b.last_error}>
                    {b.last_error}
                  </p>
                ) : null}
              </td>
              <td className="tabular px-4 py-2.5 text-right text-slate-300">
                {b.balance === null ? '—' : b.balance.toFixed(2)}
              </td>
              <td className="px-4 py-2.5 text-slate-400">{formatDate(b.last_synced_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function BotsTab({ bots }: { bots: AdminBot[] }) {
  if (!bots.length) {
    return <p className="card text-sm text-slate-400">No bots on the platform yet.</p>;
  }
  return (
    <div className="card overflow-x-auto p-0">
      <table className="w-full text-sm">
        <thead className="border-b border-edge text-xs uppercase tracking-wide text-slate-400">
          <tr>
            <th className="px-4 py-2.5 text-left font-medium">Bot</th>
            <th className="px-4 py-2.5 text-left font-medium">Owner</th>
            <th className="px-4 py-2.5 text-left font-medium">Symbol</th>
            <th className="px-4 py-2.5 text-left font-medium">Mode</th>
            <th className="px-4 py-2.5 text-left font-medium">State</th>
            <th className="px-4 py-2.5 text-left font-medium">Live grant</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-edge">
          {bots.map((b) => (
            <tr key={b.id}>
              <td className="px-4 py-2.5">
                <p className="font-medium text-slate-100">{b.name}</p>
                <p className="text-xs text-slate-500">
                  {b.strategy} · {b.timeframe}
                </p>
              </td>
              <td className="px-4 py-2.5 text-slate-300">{b.user_email}</td>
              <td className="px-4 py-2.5 text-slate-300">{b.symbol}</td>
              <td className="px-4 py-2.5">
                <span className={b.mode === 'LIVE' ? 'text-down' : 'text-slate-300'}>{b.mode}</span>
              </td>
              <td className="px-4 py-2.5">
                <span className={b.state === 'RUNNING' ? 'text-up' : 'text-slate-400'}>{b.state}</span>
              </td>
              <td className="px-4 py-2.5">
                {b.live_granted ? (
                  <span className="text-down">granted</span>
                ) : (
                  <span className="text-slate-500">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function AdminPage() {
  const { user, ready } = useAuth();
  const [stats, setStats] = useState<PlatformStats | null>(null);
  const [brokers, setBrokers] = useState<AdminBroker[]>([]);
  const [bots, setBots] = useState<AdminBot[]>([]);
  const [selfId, setSelfId] = useState('');
  const [tab, setTab] = useState<Tab>('users');
  const [error, setError] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const [loading, setLoading] = useState(true);

  const isAdmin = user?.role === 'ADMIN' || user?.role === 'SUPER_ADMIN';

  useEffect(() => {
    if (!ready) return;
    // The API is the real boundary; this avoids showing an admin shell to
    // someone who is about to be refused.
    if (!isAdmin) {
      setDenied(true);
      setLoading(false);
      return;
    }
    let cancelled = false;
    (async () => {
      setError(null);
      try {
        const me = await api<{ id: string }>('/auth/me');
        const [s, b, botsRes] = await Promise.all([
          adminApi.stats(),
          adminApi.brokers(),
          adminApi.bots(),
        ]);
        if (cancelled) return;
        setSelfId(me.id);
        setStats(s);
        setBrokers(b);
        setBots(botsRes);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Could not load admin data');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isAdmin, ready]);

  if (denied) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-semibold tracking-tight text-slate-50">Administration</h1>
        <div className="card space-y-2">
          <p className="text-sm font-medium text-slate-100">
            This account does not have administrator access.
          </p>
          <p className="text-sm text-slate-400">
            Administration is limited to accounts with the ADMIN or SUPER_ADMIN role. If
            you believe this is wrong, ask an existing administrator to grant it.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-slate-50">Administration</h1>
        <p className="mt-1 text-sm text-slate-400">
          Platform-wide view of users, broker connections and bots.
        </p>
      </header>

      {error ? <ErrorNote message={error} /> : null}
      {loading ? <Spinner label="Loading platform state" /> : null}

      {stats ? (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <Stat label="Users" value={stats.users} sub={`${stats.active_users} active`} />
          <Stat label="Bots" value={stats.bots} sub={`${stats.running_bots} running`} />
          <Stat
            label="Broker connections"
            value={stats.broker_connections}
            sub={`${stats.connected_brokers} connected`}
          />
          <Stat
            label="Trades"
            value={stats.trades}
            sub={`${stats.open_positions} open positions`}
          />
        </div>
      ) : null}

      {!loading && !error ? (
        <>
          <div className="flex gap-1 rounded-lg border border-edge bg-base-900 p-1">
            {TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => setTab(t.id)}
                aria-pressed={tab === t.id}
                className={`flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition ${
                  tab === t.id
                    ? 'bg-brand-500/15 text-brand-400'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>

          {tab === 'users' ? <UsersTab selfId={selfId} /> : null}
          {tab === 'brokers' ? <BrokersTab brokers={brokers} /> : null}
          {tab === 'bots' ? <BotsTab bots={bots} /> : null}
        </>
      ) : null}
    </div>
  );
}
