import type { Portfolio } from "../types";
import { TrendingUp, TrendingDown, DollarSign, Target } from "lucide-react";

export function PortfolioCard({ portfolio }: { portfolio: Portfolio }) {
  const isPnlPositive = portfolio.pnlDay >= 0;

  return (
    <div className="grid grid-cols-1 md:grid-cols-4 gap-4 w-full">
      <div className="bg-white dark:bg-gray-800 p-4 rounded-xl shadow-sm border border-gray-100 dark:border-gray-700">
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm text-gray-500 dark:text-gray-400 font-medium">Net Equity</span>
          <DollarSign className="w-4 h-4 text-indigo-500" />
        </div>
        <div className="text-2xl font-bold">${portfolio.equity.toLocaleString()}</div>
        <div className="text-xs text-gray-400 mt-1">Balance: ${portfolio.balance.toLocaleString()}</div>
      </div>

      <div className="bg-white dark:bg-gray-800 p-4 rounded-xl shadow-sm border border-gray-100 dark:border-gray-700">
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm text-gray-500 dark:text-gray-400 font-medium">Daily PnL</span>
          {isPnlPositive ? <TrendingUp className="w-4 h-4 text-green-500" /> : <TrendingDown className="w-4 h-4 text-red-500" />}
        </div>
        <div className={`text-2xl font-bold ${isPnlPositive ? 'text-green-500' : 'text-red-500'}`}>
          {isPnlPositive ? '+' : ''}${portfolio.pnlDay.toLocaleString()}
        </div>
        <div className={`text-xs mt-1 ${isPnlPositive ? 'text-green-600' : 'text-red-600'}`}>
          {isPnlPositive ? '+' : ''}{portfolio.pnlDayPercent}% today
        </div>
      </div>

      <div className="bg-white dark:bg-gray-800 p-4 rounded-xl shadow-sm border border-gray-100 dark:border-gray-700">
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm text-gray-500 dark:text-gray-400 font-medium">Win Rate</span>
          <Target className="w-4 h-4 text-orange-500" />
        </div>
        <div className="text-2xl font-bold">{portfolio.winRate}%</div>
        <div className="w-full bg-gray-200 dark:bg-gray-700 h-1.5 rounded-full mt-3">
          <div 
            className="bg-orange-500 h-1.5 rounded-full" 
            style={{ width: `${portfolio.winRate}%` }}
          />
        </div>
      </div>

      <div className="bg-white dark:bg-gray-800 p-4 rounded-xl shadow-sm border border-gray-100 dark:border-gray-700 flex flex-col justify-center">
        <div className="text-xs text-gray-500 dark:text-gray-400 font-medium uppercase tracking-wider">Status</div>
        <div className="flex items-center gap-2 mt-1">
          <div className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
          <span className="font-semibold text-green-600 dark:text-green-400">Live Trading</span>
        </div>
        <div className="text-[10px] text-gray-400 mt-1">Sniper Mode Active</div>
      </div>
    </div>
  );
}
