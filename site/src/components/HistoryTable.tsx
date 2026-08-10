import type { Trade } from "../types";
import { ArrowUpRight, ArrowDownRight, Clock } from "lucide-react";

export function HistoryTable({ trades }: { trades: Trade[] }) {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-xl shadow-sm border border-gray-100 dark:border-gray-700 overflow-hidden">
      <div className="p-4 border-b border-gray-50 dark:border-gray-700 flex items-center justify-between">
        <h3 className="font-bold flex items-center gap-2">
          <Clock className="w-4 h-4 text-indigo-500" />
          Recent Trade History
        </h3>
        <button className="text-xs text-indigo-600 dark:text-indigo-400 font-semibold hover:underline">
          View All
        </button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 dark:bg-gray-900/50 text-gray-500 dark:text-gray-400 uppercase text-[10px] font-bold">
            <tr>
              <th className="px-4 py-3">Asset</th>
              <th className="px-4 py-3">Side</th>
              <th className="px-4 py-3">Entry</th>
              <th className="px-4 py-3">Exit</th>
              <th className="px-4 py-3">PnL</th>
              <th className="px-4 py-3 text-right">Time</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50 dark:divide-gray-700">
            {trades.map((trade) => (
              <tr key={trade.id} className="hover:bg-gray-50/50 dark:hover:bg-gray-700/30 transition-colors">
                <td className="px-4 py-3 font-bold">{trade.symbol}</td>
                <td className="px-4 py-3">
                  <span className={`flex items-center gap-1 ${trade.side === 'buy' ? 'text-green-500' : 'text-red-500'}`}>
                    {trade.side === 'buy' ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownRight className="w-3 h-3" />}
                    {trade.side.toUpperCase()}
                  </span>
                </td>
                <td className="px-4 py-3 font-mono">${trade.entryPrice.toLocaleString()}</td>
                <td className="px-4 py-3 font-mono">
                  {trade.exitPrice ? `$${trade.exitPrice.toLocaleString()}` : '—'}
                </td>
                <td className={`px-4 py-3 font-mono font-bold ${trade.pnl && trade.pnl > 0 ? 'text-green-500' : trade.pnl && trade.pnl < 0 ? 'text-red-500' : 'text-gray-400'}`}>
                  {trade.pnl ? `${trade.pnl > 0 ? '+' : ''}${trade.pnl.toFixed(2)}` : '—'}
                </td>
                <td className="px-4 py-3 text-right text-gray-400 text-xs">
                  {new Date(trade.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
