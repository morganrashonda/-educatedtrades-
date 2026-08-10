import { TrendingDown, TrendingUp, AlertTriangle, Gauge } from "lucide-react";

export function DailyLossTracker({
  dailyPnlPct,
  dailyLossLimit,
  dailyLossHit,
}: {
  dailyPnlPct: number;
  dailyLossLimit: number;
  dailyLossHit: boolean;
}) {
  const isPositive = dailyPnlPct >= 0;
  const isNearLimit = !isPositive && Math.abs(dailyPnlPct) >= dailyLossLimit * 0.75;
  const absPnl = Math.abs(dailyPnlPct);
  const usagePercent = dailyLossLimit > 0 ? Math.min((absPnl / dailyLossLimit) * 100, 100) : 0;

  // Determine styles
  let barColor = 'bg-gradient-to-r from-green-500 to-green-bright';
  let iconBg = 'bg-green-bright/10';
  let iconColor = 'text-green-bright';
  let borderStyle = 'border-trade-600/20';

  if (dailyLossHit) {
    barColor = 'bg-gradient-to-r from-red-500 to-red-bright';
    iconBg = 'bg-red-bright/10';
    iconColor = 'text-red-bright';
    borderStyle = 'border-red-bright/30 card-border-glow';
  } else if (isNearLimit && !isPositive) {
    barColor = 'bg-gradient-to-r from-red-500 to-red-bright';
    iconBg = 'bg-red-bright/10';
    iconColor = 'text-red-bright';
    borderStyle = 'border-red-bright/20';
  } else if (!isPositive) {
    barColor = 'bg-gradient-to-r from-red-400 to-red-500';
    iconBg = 'bg-red-bright/5';
    iconColor = 'text-red-400';
  }

  return (
    <div className={`card p-5 transition-all duration-300 ${borderStyle} ${dailyLossHit ? 'glow-red' : ''}`}>
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2.5">
          <div className={`w-9 h-9 rounded-lg flex items-center justify-center ${iconBg}`}>
            {isPositive
              ? <TrendingUp className={`w-5 h-5 ${iconColor}`} />
              : <TrendingDown className={`w-5 h-5 ${iconColor}`} />
            }
          </div>
          <div>
            <h3 className="font-bold text-sm text-trade-100">Daily P&L</h3>
            <span className="text-[10px] text-trade-400">Session tracking</span>
          </div>
        </div>
        <div className={`text-xl font-bold font-mono ${
          isPositive ? 'text-green-bright' : dailyLossHit ? 'text-red-bright animate-pulse' : 'text-red-bright'
        }`}>
          {isPositive ? '+' : ''}{dailyPnlPct.toFixed(2)}%
        </div>
      </div>

      {/* Progress bar */}
      <div className="space-y-3">
        <div className="flex justify-between text-xs">
          <span className="text-trade-400 font-medium">
            <span className={isPositive ? 'text-green-bright' : 'text-red-bright'}>
              {isPositive ? 'Profit' : `${absPnl.toFixed(2)}%`}
            </span>
            {!isPositive && <span className="text-trade-500"> of {dailyLossLimit}% limit</span>}
          </span>
          <span className={`font-bold font-mono text-xs ${
            isPositive ? 'text-green-bright' : dailyLossHit ? 'text-red-bright' : isNearLimit ? 'text-red-bright' : 'text-trade-300'
          }`}>
            {usagePercent.toFixed(0)}%
          </span>
        </div>

        <div className="progress-bar h-2.5">
          <div
            className={`progress-bar-fill ${barColor} ${dailyLossHit ? 'animate-flash-danger' : isNearLimit ? 'animate-pulse' : ''}`}
            style={{ width: `${isPositive ? 0 : usagePercent}%` }}
          />
        </div>

        {/* Limit marker */}
        {!isPositive && (
          <div className="relative h-0">
            <div
              className="absolute top-0 w-0.5 h-3 bg-trade-400/50 rounded-full"
              style={{ left: `100%` }}
            />
          </div>
        )}
      </div>

      {/* Daily loss hit warning */}
      {dailyLossHit && (
        <div className="mt-4 p-3 rounded-lg bg-red-bright/5 border border-red-bright/20">
          <div className="flex items-center gap-2.5">
            <AlertTriangle className="w-4.5 h-4.5 text-red-bright shrink-0 animate-pulse" />
            <div>
              <span className="text-xs font-bold text-red-bright">DAILY LOSS LIMIT REACHED</span>
              <p className="text-[10px] text-trade-400 mt-0.5">Trading halted — max loss of {dailyLossLimit}% hit</p>
            </div>
          </div>
        </div>
      )}

      {/* Summary text */}
      <div className="mt-3 flex items-center gap-1.5">
        <Gauge className="w-3 h-3 text-trade-500" />
        <span className="text-[10px] text-trade-500">
          {isPositive
            ? `On track — profit for the day`
            : dailyLossHit
              ? `Max loss of ${dailyLossLimit}% hit — trading stopped`
              : `Loss limit: ${dailyLossLimit}%`
          }
        </span>
      </div>
    </div>
  );
}