import { AnimatedCounter } from "./AnimatedCounter";
import {
  TrendingUp,
  TrendingDown,
  BarChart3,
  Activity,
  Zap,
  Award,
  DollarSign,
  Target,
} from "lucide-react";

type QuickStatsProps = {
  totalTrades?: number;
  winRate?: number;
  totalPnl?: number;
  avgProfit?: number;
  tradesToday?: number;
  activeSignals?: number;
  patternCount?: number;
};

export function QuickStats({
  totalTrades = 0,
  winRate = 0,
  totalPnl = 0,
  avgProfit = 0,
  tradesToday = 0,
  activeSignals = 0,
  patternCount = 0,
}: QuickStatsProps) {
  const isPnlPositive = totalPnl >= 0;

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-7 gap-3">
      {/* Total Trades */}
      <div className="glass-panel p-2.5 card-hover-lift">
        <div className="flex items-center justify-between mb-1">
          <span className="text-[9px] text-trade-400 font-medium uppercase tracking-wider">Total Trades</span>
          <Activity className="w-3 h-3 text-accent-400" />
        </div>
        <div className="font-bold font-mono text-sm text-trade-100">
          <AnimatedCounter value={totalTrades} />
        </div>
      </div>

      {/* Win Rate */}
      <div className="glass-panel p-2.5 card-hover-lift">
        <div className="flex items-center justify-between mb-1">
          <span className="text-[9px] text-trade-400 font-medium uppercase tracking-wider">Win Rate</span>
          <Target className="w-3 h-3 text-amber-bright" />
        </div>
        <div className="font-bold font-mono text-sm text-trade-100">
          <AnimatedCounter value={winRate} suffix="%" decimals={1} />
        </div>
      </div>

      {/* Total P&L */}
      <div className="glass-panel p-2.5 card-hover-lift">
        <div className="flex items-center justify-between mb-1">
          <span className="text-[9px] text-trade-400 font-medium uppercase tracking-wider">Total P&L</span>
          <DollarSign className="w-3 h-3 text-green-bright" />
        </div>
        <div className={`font-bold font-mono text-sm ${isPnlPositive ? 'text-green-bright' : 'text-red-bright'}`}>
          {isPnlPositive ? '+' : ''}<AnimatedCounter value={Math.abs(totalPnl)} prefix="$" decimals={2} />
        </div>
      </div>

      {/* Avg Profit */}
      <div className="glass-panel p-2.5 card-hover-lift">
        <div className="flex items-center justify-between mb-1">
          <span className="text-[9px] text-trade-400 font-medium uppercase tracking-wider">Avg Profit</span>
          <BarChart3 className="w-3 h-3 text-accent-400" />
        </div>
        <div className="font-bold font-mono text-sm text-trade-100">
          <AnimatedCounter value={avgProfit} prefix="$" decimals={2} />
        </div>
      </div>

      {/* Today's Trades */}
      <div className="glass-panel p-2.5 card-hover-lift">
        <div className="flex items-center justify-between mb-1">
          <span className="text-[9px] text-trade-400 font-medium uppercase tracking-wider">Today</span>
          <Zap className="w-3 h-3 text-amber-bright" />
        </div>
        <div className="font-bold font-mono text-sm text-trade-100">
          <AnimatedCounter value={tradesToday} suffix=" trades" />
        </div>
      </div>

      {/* Active Signals */}
      <div className="glass-panel p-2.5 card-hover-lift">
        <div className="flex items-center justify-between mb-1">
          <span className="text-[9px] text-trade-400 font-medium uppercase tracking-wider">Signals</span>
          <TrendingUp className="w-3 h-3 text-green-bright" />
        </div>
        <div className="font-bold font-mono text-sm text-trade-100">
          <AnimatedCounter value={activeSignals} />
        </div>
      </div>

      {/* Patterns Learned */}
      <div className="glass-panel p-2.5 card-hover-lift">
        <div className="flex items-center justify-between mb-1">
          <span className="text-[9px] text-trade-400 font-medium uppercase tracking-wider">Patterns</span>
          <Award className="w-3 h-3 text-accent-400" />
        </div>
        <div className="font-bold font-mono text-sm text-trade-100">
          <AnimatedCounter value={patternCount} />
        </div>
      </div>
    </div>
  );
}