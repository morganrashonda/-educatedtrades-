import type { Trade } from "../types";
import { ArrowUpRight, ArrowDownRight, Clock, Eye, ExternalLink } from "lucide-react";

export function HistoryTable({ trades }: { trades: Trade[] }) {
  return (
    <div className="card overflow-hidden">
      <div className="p-5 border-b border-trade-600/20 flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-lg bg-accent-500/10 flex items-center justify-center">
            <Clock className="w-4.5 h-4.5 text-accent-400" />
          </div>
          <div>
            <h3 className="font-bold text-sm text-trade-100">Recent Trade History</h3>
            <span className="text-[10px] text-trade-400">Last {trades.length} trades</span>
          </div>
        </div>
        <button className="flex items-center gap-1.5 text-xs text-accent-400 font-semibold hover:text-accent-300 transition-colors px-3 py-1.5 rounded-lg hover:bg-accent-500/5">
          View All
          <ExternalLink className="w-3 h-3" />
        </button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left">
          <thead>
            <tr className="table-header">
              <th className="table-cell">Asset</th>
              <th className="table-cell">Side</th>
              <th className="table-cell">Entry</th>
              <th className="table-cell">Exit</th>
              <th className="table-cell">PnL</th>
              <th className="table-cell text-right">Time</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-trade-600/20">
            {trades.map((trade) => (
              <tr key={trade.id} className="table-row">
                <td className="table-cell font-bold text-trade-100">{trade.symbol}</td>
                <td className="table-cell">
                  <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[10px] font-bold uppercase tracking-wider border ${
                    trade.side === 'buy' 
                      ? 'text-green-bright bg-green-bright/5 border-green-bright/20' 
                      : 'text-red-bright bg-red-bright/5 border-red-bright/20'
                  }`}>
                    {trade.side === 'buy' ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownRight className="w-3 h-3" />}
                    {trade.side}
                  </span>
                </td>
                <td className="table-cell font-mono text-xs text-trade-200">${trade.entryPrice.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                <td className="table-cell font-mono text-xs">
                  {trade.exitPrice ? (
                    <span className="text-trade-200">${trade.exitPrice.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                  ) : (
                    <span className="text-trade-500">—</span>
                  )}
                </td>
                <td className="table-cell">
                  {trade.pnl !== undefined && trade.pnl !== null ? (
                    <span className={`font-mono font-bold text-xs ${trade.pnl > 0 ? 'text-green-bright' : trade.pnl < 0 ? 'text-red-bright' : 'text-trade-400'}`}>
                      {trade.pnl > 0 ? '+' : ''}{trade.pnl.toFixed(2)}
                    </span>
                  ) : (
                    <span className="text-trade-500">—</span>
                  )}
                </td>
                <td className="table-cell text-right text-[10px] text-trade-500 font-mono">
                  {new Date(trade.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                </td>
              </tr>
            ))}
            {trades.length === 0 && (
              <tr>
                <td colSpan={6} className="text-center py-10">
                  <div className="flex flex-col items-center gap-3">
                    <div className="w-12 h-12 rounded-xl bg-trade-700/30 flex items-center justify-center">
                      <Clock className="w-6 h-6 text-trade-400" />
                    </div>
                    <p className="text-sm text-trade-400">No trades yet</p>
                  </div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}