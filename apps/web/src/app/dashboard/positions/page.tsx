'use client';

import { useCallback, useEffect, useState } from 'react';

import { EmptyState, ErrorNote, ModeBadge, Pnl, Spinner } from '@/components/ui';
import { botsApi, tradingApi } from '@/lib/api';
import type { Position } from '@/lib/types';

export default function PositionsPage() {
  const [rows, setRows] = useState<Position[]>([]);
  const [openOnly, setOpenOnly] = useState(true);
  const [error, setError] = useState('');
  const [busyId, setBusyId] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  const load = useCallback(async () => {
    try {
      setRows(await tradingApi.positions(openOnly));
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load positions');
    } finally {
      setReady(true);
    }
  }, [openOnly]);

  useEffect(() => { void load(); }, [load]);

  async function close(p: Position) {
    if (!confirm(`Close ${p.symbol} ${p.side} ${Number(p.volume).toFixed(2)}?`)) return;
    setBusyId(p.id);
    try {
      await botsApi.closePosition(p.id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Close failed');
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold text-slate-50">Positions</h1>
        <div className="flex gap-1 rounded-lg border border-edge bg-base-900 p-1">
          {([true, false] as const).map((v) => (
            <button key={String(v)} onClick={() => setOpenOnly(v)}
              className={`rounded-md px-3 py-1.5 text-xs font-medium transition ${
                openOnly === v ? 'bg-brand-500 text-slate-950' : 'text-slate-400 hover:text-slate-200'
              }`}>
              {v ? 'Open' : 'All'}
            </button>
          ))}
        </div>
      </div>

      {error ? <ErrorNote message={error} /> : null}

      {!ready ? (
        <Spinner />
      ) : rows.length === 0 ? (
        <EmptyState title={openOnly ? 'No open positions' : 'No positions yet'}
          hint="Positions appear here once a bot opens one." />
      ) : (
        <>
          {/* Mobile: cards. Desktop: table. */}
          <ul className="space-y-3 lg:hidden">
            {rows.map((p) => (
              <li key={p.id} className="card">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <p className="font-medium text-slate-100">{p.symbol}</p>
                    <p className="text-xs text-slate-500">
                      {p.side} {Number(p.volume).toFixed(2)} · {p.status}
                    </p>
                  </div>
                  <div className="flex flex-col items-end gap-1">
                    <Pnl value={p.unrealized_pnl} />
                    <ModeBadge mode={p.mode} />
                  </div>
                </div>
                <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                  <div className="flex justify-between"><dt className="text-slate-500">Entry</dt>
                    <dd className="tabular text-slate-300">{Number(p.open_price).toFixed(5)}</dd></div>
                  <div className="flex justify-between"><dt className="text-slate-500">Current</dt>
                    <dd className="tabular text-slate-300">{Number(p.current_price).toFixed(5)}</dd></div>
                  <div className="flex justify-between"><dt className="text-slate-500">Stop</dt>
                    <dd className="tabular text-slate-300">{p.stop_loss ? Number(p.stop_loss).toFixed(5) : '—'}</dd></div>
                  <div className="flex justify-between"><dt className="text-slate-500">Target</dt>
                    <dd className="tabular text-slate-300">{p.take_profit ? Number(p.take_profit).toFixed(5) : '—'}</dd></div>
                </dl>
                {p.status === 'OPEN' && (
                  <button className="btn-ghost mt-3 w-full" disabled={busyId === p.id}
                    onClick={() => close(p)}>Close position</button>
                )}
              </li>
            ))}
          </ul>

          <div className="card hidden overflow-hidden lg:block">
            <div className="table-scroll">
              <table>
                <thead>
                  <tr className="border-b border-edge text-xs uppercase tracking-wide text-slate-500">
                    <th className="py-2 pr-4 font-medium">Symbol</th>
                    <th className="py-2 pr-4 font-medium">Side</th>
                    <th className="py-2 pr-4 text-right font-medium">Volume</th>
                    <th className="py-2 pr-4 text-right font-medium">Entry</th>
                    <th className="py-2 pr-4 text-right font-medium">Current</th>
                    <th className="py-2 pr-4 text-right font-medium">Stop</th>
                    <th className="py-2 pr-4 text-right font-medium">Target</th>
                    <th className="py-2 pr-4 text-right font-medium">Unrealized</th>
                    <th className="py-2 pr-4 font-medium">Mode</th>
                    <th className="py-2 font-medium" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-edge">
                  {rows.map((p) => (
                    <tr key={p.id} className="hover:bg-base-850/50">
                      <td className="py-2.5 pr-4 font-medium text-slate-100">{p.symbol}</td>
                      <td className="py-2.5 pr-4">
                        <span className={p.side === 'BUY' ? 'text-up' : 'text-down'}>{p.side}</span>
                      </td>
                      <td className="tabular py-2.5 pr-4 text-right text-slate-300">
                        {Number(p.volume).toFixed(2)}
                      </td>
                      <td className="tabular py-2.5 pr-4 text-right text-slate-300">
                        {Number(p.open_price).toFixed(5)}
                      </td>
                      <td className="tabular py-2.5 pr-4 text-right text-slate-300">
                        {Number(p.current_price).toFixed(5)}
                      </td>
                      <td className="tabular py-2.5 pr-4 text-right text-slate-500">
                        {p.stop_loss ? Number(p.stop_loss).toFixed(5) : '—'}
                      </td>
                      <td className="tabular py-2.5 pr-4 text-right text-slate-500">
                        {p.take_profit ? Number(p.take_profit).toFixed(5) : '—'}
                      </td>
                      <td className="py-2.5 pr-4 text-right"><Pnl value={p.unrealized_pnl} /></td>
                      <td className="py-2.5 pr-4"><ModeBadge mode={p.mode} /></td>
                      <td className="py-2.5 text-right">
                        {p.status === 'OPEN' && (
                          <button className="text-xs font-medium text-brand-400 hover:text-brand-500"
                            disabled={busyId === p.id} onClick={() => close(p)}>Close</button>
                        )}
                      </td>
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
