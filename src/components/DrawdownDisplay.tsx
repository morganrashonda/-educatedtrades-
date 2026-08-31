import { TrendingDown, DollarSign, BarChart3, Gauge } from "lucide-react";

export function DrawdownDisplay({ equity, peakEquity: externalPeak }: { equity: number; peakEquity?: number }) {
  // The parent tracks peak equity across polls and passes it down; fall back
  // to current equity only for the first render before that prop arrives.
  const peakEquity = Math.max(externalPeak ?? equity, equity);

  const drawdownPct = peakEquity > 0 ? ((peakEquity - equity) / peakEquity) * 100 : 0;
  const drawdownAbs = peakEquity - equity;

  let severityColor = 'text-green-bright';
  let barColor = 'bg-gradient-to-r from-green-500 to-green-bright';
  let borderStyle = 'border-trade-600/20';
  let iconBg = 'bg-green-bright/10';

  if (drawdownPct > 15) {
    severityColor = 'text-red-bright';
    barColor = 'bg-gradient-to-r from-red-500 to-red-bright';
    borderStyle = 'border-red-bright/20';
    iconBg = 'bg-red-bright/10';
  } else if (drawdownPct > 8) {
    severityColor = 'text-red-400';
    barColor = 'bg-gradient-to-r from-orange-500 to-red-500';
    borderStyle = 'border-red-bright/10';
    iconBg = 'bg-red-bright/5';
  } else if (drawdownPct > 3) {
    severityColor = 'text-amber-bright';
    barColor = 'bg-gradient-to-r from-amber-500 to-orange-500';
    borderStyle = 'border-amber-bright/10';
    iconBg = 'bg-amber-bright/5';
  } else if (drawdownPct > 0) {
    severityColor = 'text-amber-400';
    barColor = 'bg-gradient-to-r from-amber-400 to-yellow-500';
  }

  return (
    <div className={`card p-5 transition-all duration-300 ${borderStyle}`}>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2.5">
          <div className={`w-9 h-9 rounded-lg flex items-center justify-center ${iconBg}`}>
            <BarChart3 className={`w-5 h-5 ${severityColor}`} />
          </div>
          <div>
            <h3 className="font-bold text-sm text-trade-100">Drawdown</h3>
            <span className="text-[10px] text-trade-400">Peak-to-trough decline</span>
          </div>
        </div>
        <div className={`text-xl font-bold font-mono ${drawdownPct > 0 ? severityColor : 'text-green-bright'}`}>
          {drawdownPct > 0 ? '-' : ''}{drawdownPct.toFixed(2)}%
        </div>
      </div>

      {/* Visual bar */}
      <div className="progress-bar h-2.5 mb-4">
        <div
          className={`progress-bar-fill ${barColor}`}
          style={{ width: `${Math.min(drawdownPct, 30)}%` }}
        />
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="p-3 rounded-lg bg-trade-700/30">
          <div className="flex items-center gap-1.5 text-[10px] text-trade-400 mb-1.5">
            <DollarSign className="w-3 h-3" />
            <span>Drawdown ($)</span>
          </div>
          <span className={`font-mono font-bold text-sm ${drawdownPct > 0 ? severityColor : 'text-green-bright'}`}>
            ${drawdownAbs.toFixed(2)}
          </span>
        </div>

        <div className="p-3 rounded-lg bg-trade-700/30">
          <div className="flex items-center gap-1.5 text-[10px] text-trade-400 mb-1.5">
            <TrendingDown className="w-3 h-3" />
            <span>Peak Equity</span>
          </div>
          <span className="font-mono font-bold text-sm text-trade-100">
            ${peakEquity.toLocaleString()}
          </span>
        </div>
      </div>

      <div className="mt-3 flex items-center gap-1.5">
        <Gauge className="w-3 h-3 text-trade-500" />
        <span className="text-[10px] text-trade-500">
          Current equity: ${equity.toLocaleString()}
        </span>
      </div>
    </div>
  );
}