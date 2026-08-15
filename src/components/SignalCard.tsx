import type { Signal } from "../types";
import { Zap, Brain, TrendingUp, Signal as SignalIcon } from "lucide-react";

export function SignalCard({ signal }: { signal: Signal }) {
  const getRecommendationColor = (rec: Signal['recommendation']) => {
    switch (rec) {
      case 'strong_buy': return 'badge-green';
      case 'buy': return 'text-green-bright bg-green-bright/5 border-green-bright/15';
      case 'sell': return 'text-red-bright bg-red-bright/5 border-red-bright/15';
      case 'strong_sell': return 'badge-red';
      default: return 'text-trade-300 bg-trade-700/50 border-trade-600/20';
    }
  };

  const getRecommendationLabel = (rec: Signal['recommendation']) => {
    return rec.replace('_', ' ').toUpperCase();
  };

  const getSentimentColor = (val: number) => {
    if (val > 0.5) return 'text-green-bright';
    if (val > 0) return 'text-green-400';
    if (val < -0.5) return 'text-red-bright';
    if (val < 0) return 'text-red-400';
    return 'text-trade-300';
  };

  return (
    <div className="card p-5">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-lg bg-yellow-500/10 flex items-center justify-center">
            <Zap className="w-5 h-5 text-yellow-400 fill-yellow-400/30" />
          </div>
          <div>
            <h3 className="font-bold text-sm text-trade-100">{signal.symbol}</h3>
            <span className="text-[10px] text-trade-400">Signal Analysis</span>
          </div>
        </div>
        <span className={`px-3 py-1 rounded-full text-[10px] font-bold border ${getRecommendationColor(signal.recommendation)}`}>
          {getRecommendationLabel(signal.recommendation)}
        </span>
      </div>

      <div className="space-y-4">
        <div>
          <div className="flex items-center justify-between text-xs mb-2">
            <span className="flex items-center gap-1.5 text-trade-400">
              <TrendingUp className="w-3.5 h-3.5" /> Sentiment Analysis
            </span>
            <span className={`font-mono font-bold text-sm ${getSentimentColor(signal.sentiment)}`}>
              {(signal.sentiment * 100).toFixed(1)}%
            </span>
          </div>
          <div className="relative w-full h-2 bg-trade-600/50 rounded-full overflow-hidden">
            <div className="absolute top-0 bottom-0 left-1/2 w-0.5 bg-trade-500" />
            <div 
              className={`absolute top-0 bottom-0 transition-all duration-1000 rounded-full ${
                signal.sentiment > 0 
                  ? 'bg-gradient-to-r from-transparent via-green-500 to-green-bright left-1/2' 
                  : 'bg-gradient-to-r from-red-bright via-red-500 to-transparent right-1/2'
              }`}
              style={{ width: `${Math.abs(signal.sentiment) * 50}%` }}
            />
          </div>
        </div>

        <div>
          <div className="flex items-center justify-between text-xs mb-2">
            <span className="flex items-center gap-1.5 text-trade-400">
              <Brain className="w-3.5 h-3.5" /> Pattern Conviction
            </span>
            <span className="font-mono font-bold text-sm text-accent-400">
              {(signal.patternConviction * 100).toFixed(1)}%
            </span>
          </div>
          <div className="progress-bar h-2">
            <div 
              className="progress-bar-fill bg-gradient-to-r from-accent-500 to-accent-400" 
              style={{ width: `${signal.patternConviction * 100}%` }}
            />
          </div>
        </div>
      </div>

      <div className="mt-4 pt-3 divider">
        <div className="flex items-center gap-1.5">
          <SignalIcon className="w-3 h-3 text-trade-500" />
          <p className="text-[10px] text-trade-500">
            Updated: {new Date(signal.timestamp).toLocaleTimeString()}
          </p>
        </div>
      </div>
    </div>
  );
}