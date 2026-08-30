import type { Portfolio } from "../types";
import { TrendingUp, TrendingDown, DollarSign, Target, Wallet, Gauge } from "lucide-react";

export function PortfolioCard({
  portfolio,
  mode,
  isHealthy,
}: {
  portfolio: Portfolio;
  mode: "manual" | "autonomous";
  isHealthy: boolean;
}) {
  const isPnlPositive = portfolio.pnlDay >= 0;

  return (
    <div className="grid grid-cols-1 md:grid-cols-4 gap-4 w-full">
      <div className="stat-card card-glow">
        <div className="flex items-center justify-between mb-3">
          <span className="metric-label">Net Equity</span>
          <div className="w-8 h-8 rounded-lg bg-accent-500/10 flex items-center justify-center">
            <DollarSign className="w-4 h-4 text-accent-400" />
          </div>
        </div>
        <div className="metric-value text-2xl lg:text-3xl text-white">${portfolio.equity.toLocaleString()}</div>
        <div className="text-xs text-trade-400 mt-2 flex items-center gap-1.5">
          <Wallet className="w-3 h-3" />
          Balance: ${portfolio.balance.toLocaleString()}
        </div>
      </div>

      <div className="stat-card">
        <div className="flex items-center justify-between mb-3">
          <span className="metric-label">Daily PnL</span>
          <div className={`w-8 h-8 rounded-lg flex items-center justify-center ${
            isPnlPositive ? 'bg-green-bright/10' : 'bg-red-bright/10'
          }`}>
            {isPnlPositive ? (
              <TrendingUp className="w-4 h-4 text-green-bright" />
            ) : (
              <TrendingDown className="w-4 h-4 text-red-bright" />
            )}
          </div>
        </div>
        <div className={`metric-value text-2xl lg:text-3xl ${isPnlPositive ? 'text-green-bright' : 'text-red-bright'}`}>
          {isPnlPositive ? '+' : ''}${portfolio.pnlDay.toLocaleString()}
        </div>
        <div className={`text-xs font-semibold mt-2 flex items-center gap-1.5 ${isPnlPositive ? 'text-green-bright/70' : 'text-red-bright/70'}`}>
          {isPnlPositive ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
          {isPnlPositive ? '+' : ''}{portfolio.pnlDayPercent}% today
        </div>
      </div>

      <div className="stat-card">
        <div className="flex items-center justify-between mb-3">
          <span className="metric-label">Win Rate</span>
          <div className="w-8 h-8 rounded-lg bg-amber-bright/10 flex items-center justify-center">
            <Target className="w-4 h-4 text-amber-bright" />
          </div>
        </div>
        <div className="metric-value text-2xl lg:text-3xl text-white">{portfolio.winRate}%</div>
        <div className="mt-3 progress-bar">
          <div 
            className="progress-bar-fill bg-gradient-to-r from-amber-500 to-green-bright" 
            style={{ width: `${portfolio.winRate}%` }}
          />
        </div>
      </div>

      <div className="stat-card">
        <div className="flex items-center justify-between mb-3">
          <span className="metric-label">Status</span>
          <div className={`w-8 h-8 rounded-lg flex items-center justify-center ${
            isHealthy ? 'bg-green-bright/10' : 'bg-red-bright/10'
          }`}>
            <Gauge className={`w-4 h-4 ${isHealthy ? 'text-green-bright' : 'text-red-bright'}`} />
          </div>
        </div>
        <div className="flex items-center gap-2.5 mt-1">
          <span className={`w-2.5 h-2.5 rounded-full ${
            isHealthy ? 'bg-green-bright animate-pulse shadow-lg shadow-green-bright/30' : 'bg-red-bright'
          }`} />
          <span className={`font-semibold text-sm ${isHealthy ? 'text-green-bright' : 'text-red-bright'}`}>
            {isHealthy ? 'Live Trading' : 'Trading Halted'}
          </span>
        </div>
        <div className="flex items-center gap-1.5 mt-2">
          <div className="rounded-full bg-accent-500/10 px-2.5 py-0.5">
            <span className="text-[10px] font-bold text-accent-400 uppercase tracking-wider">
              {mode === 'autonomous' ? 'Sniper Mode' : 'Manual Mode'}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}