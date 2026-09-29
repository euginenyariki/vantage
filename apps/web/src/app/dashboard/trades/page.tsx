'use client';

import { useCallback, useEffect, useState } from 'react';

import { EmptyState, ErrorNote, ModeBadge, Pnl, Spinner } from '@/components/ui';
import { tradingApi } from '@/lib/api';
import type { Trade } from '@/lib/types';

export default function TradesPage() {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [error, setError] = useState('');
  const [ready, setReady] = useState(false);

  const load = useCallback(async () => {
    try {
      setTrades(await tradingApi.trades());
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load trades');
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const wins = trades.filter((t) => parseFloat(t.pnl) > 0).length;
  const net = trades.reduce((s, t) => s + parseFloat(t.pnl), 0);

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold text-slate-50">Trade history</h1>
      {error ? <ErrorNote message={error} /> : null}

      {trades.length > 0 && (
        <div className="grid grid-cols-3 gap-3">
          <div className="card">
            <p className="text-xs uppercase tracking-wide text-slate-400">Trades</p>
            <p className="stat-value mt-1">{trades.length}</p>
          </div>
          <div className="card">
            <p className="text-xs uppercase tracking-wide text-slate-400">Win rate</p>
            <p className="stat-value mt-1">{((wins / trades.length) * 100).toFixed(1)}%</p>
          </div>
          <div className="card">
            <p className="text-xs uppercase tracking-wide text-slate-400">Net P/L</p>
            <p className="stat-value mt-1"><Pnl value={net} /></p>
          </div>
        </div>
      )}

      {!ready ? (
        <Spinner />
      ) : trades.length === 0 ? (
        <EmptyState title="No trades yet"
          hint="Closed trades and their results will be listed here." />
      ) : (
        <>
          <ul className="space-y-3 lg:hidden">
            {trades.map((t) => (
              <li key={t.id} className="card">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <p className="font-medium text-slate-100">{t.symbol}</p>
                    <p className="text-xs text-slate-500">
                      {new Date(t.closed_at).toLocaleString()}
                    </p>
                  </div>
                  <div className="flex flex-col items-end gap-1">
                    <Pnl value={t.pnl} />
                    <span className={`text-[11px] font-medium ${parseFloat(t.pnl) >= 0 ? 'text-up' : 'text-down'}`}>
                      {t.result}
                    </span>
                  </div>
                </div>
                <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                  <div className="flex justify-between"><dt className="text-slate-500">Side</dt>
                    <dd className="text-slate-300">{t.side} {Number(t.volume).toFixed(2)}</dd></div>
                  <div className="flex justify-between"><dt className="text-slate-500">R</dt>
                    <dd className="tabular text-slate-300">{t.r_multiple ? Number(t.r_multiple).toFixed(2) : '—'}</dd></div>
                  <div className="flex justify-between"><dt className="text-slate-500">In</dt>
                    <dd className="tabular text-slate-300">{Number(t.open_price).toFixed(5)}</dd></div>
                  <div className="flex justify-between"><dt className="text-slate-500">Out</dt>
                    <dd className="tabular text-slate-300">{Number(t.close_price).toFixed(5)}</dd></div>
                </dl>
              </li>
            ))}
          </ul>

          <div className="card hidden overflow-hidden lg:block">
            <div className="table-scroll">
              <table>
                <thead>
                  <tr className="border-b border-edge text-xs uppercase tracking-wide text-slate-500">
                    <th className="py-2 pr-4 font-medium">Closed</th>
                    <th className="py-2 pr-4 font-medium">Symbol</th>
                    <th className="py-2 pr-4 font-medium">Side</th>
                    <th className="py-2 pr-4 text-right font-medium">Volume</th>
                    <th className="py-2 pr-4 text-right font-medium">In</th>
                    <th className="py-2 pr-4 text-right font-medium">Out</th>
                    <th className="py-2 pr-4 text-right font-medium">R</th>
                    <th className="py-2 pr-4 text-right font-medium">P/L</th>
                    <th className="py-2 pr-4 font-medium">Result</th>
                    <th className="py-2 font-medium">Mode</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-edge">
                  {trades.map((t) => (
                    <tr key={t.id} className="hover:bg-base-850/50">
                      <td className="py-2.5 pr-4 text-slate-400">
                        {new Date(t.closed_at).toLocaleString()}
                      </td>
                      <td className="py-2.5 pr-4 font-medium text-slate-100">{t.symbol}</td>
                      <td className="py-2.5 pr-4">
                        <span className={t.side === 'BUY' ? 'text-up' : 'text-down'}>{t.side}</span>
                      </td>
                      <td className="tabular py-2.5 pr-4 text-right text-slate-300">
                        {Number(t.volume).toFixed(2)}
                      </td>
                      <td className="tabular py-2.5 pr-4 text-right text-slate-300">
                        {Number(t.open_price).toFixed(5)}
                      </td>
                      <td className="tabular py-2.5 pr-4 text-right text-slate-300">
                        {Number(t.close_price).toFixed(5)}
                      </td>
                      <td className="tabular py-2.5 pr-4 text-right text-slate-400">
                        {t.r_multiple ? Number(t.r_multiple).toFixed(2) : '—'}
                      </td>
                      <td className="py-2.5 pr-4 text-right"><Pnl value={t.pnl} /></td>
                      <td className="py-2.5 pr-4 text-xs text-slate-400">{t.result}</td>
                      <td className="py-2.5"><ModeBadge mode={t.mode} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
