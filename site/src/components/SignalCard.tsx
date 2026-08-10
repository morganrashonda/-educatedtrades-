import type { Signal } from "../types";
import { Zap, Brain, TrendingUp, TrendingDown } from "lucide-react";

export function SignalCard({ signal }: { signal: Signal }) {
  const getRecommendationColor = (rec: Signal['recommendation']) => {
    switch (rec) {
      case 'strong_buy': return 'text-green-600 bg-green-50 border-green-200 dark:bg-green-900/20 dark:border-green-800';
      case 'buy': return 'text-green-500 bg-green-50/50 border-green-100 dark:bg-green-900/10 dark:border-green-800/50';
      case 'sell': return 'text-red-500 bg-red-50/50 border-red-100 dark:bg-red-900/10 dark:border-red-800/50';
      case 'strong_sell': return 'text-red-600 bg-red-50 border-red-200 dark:bg-red-900/20 dark:border-red-800';
      default: return 'text-gray-500 bg-gray-50 border-gray-200 dark:bg-gray-800 dark:border-gray-700';
    }
  };

  const getRecommendationLabel = (rec: Signal['recommendation']) => {
    return rec.replace('_', ' ').toUpperCase();
  };

  return (
    <div className="bg-white dark:bg-gray-800 p-5 rounded-xl shadow-sm border border-gray-100 dark:border-gray-700">
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-bold text-lg flex items-center gap-2">
          <Zap className="w-5 h-5 text-yellow-500 fill-yellow-500" />
          {signal.symbol} Signal
        </h3>
        <span className={`px-3 py-1 rounded-full text-xs font-bold border ${getRecommendationColor(signal.recommendation)}`}>
          {getRecommendationLabel(signal.recommendation)}
        </span>
      </div>

      <div className="space-y-4">
        <div>
          <div className="flex items-center justify-between text-sm mb-1">
            <span className="flex items-center gap-1.5 text-gray-500 dark:text-gray-400">
              <TrendingUp className="w-3.5 h-3.5" /> Sentiment Analysis
            </span>
            <span className={`font-mono font-bold ${signal.sentiment > 0 ? 'text-green-500' : signal.sentiment < 0 ? 'text-red-500' : 'text-gray-500'}`}>
              {(signal.sentiment * 100).toFixed(1)}%
            </span>
          </div>
          <div className="relative w-full h-2 bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden">
            <div 
              className={`absolute top-0 bottom-0 transition-all duration-1000 ${signal.sentiment > 0 ? 'bg-green-500 left-1/2' : 'bg-red-500 right-1/2'}`}
              style={{ width: `${Math.abs(signal.sentiment) * 50}%` }}
            />
            <div className="absolute top-0 bottom-0 left-1/2 w-0.5 bg-gray-300 dark:bg-gray-500" />
          </div>
        </div>

        <div>
          <div className="flex items-center justify-between text-sm mb-1">
            <span className="flex items-center gap-1.5 text-gray-500 dark:text-gray-400">
              <Brain className="w-3.5 h-3.5" /> Pattern Conviction
            </span>
            <span className="font-mono font-bold text-indigo-500">
              {(signal.patternConviction * 100).toFixed(1)}%
            </span>
          </div>
          <div className="w-full h-2 bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden">
            <div 
              className="h-full bg-indigo-500 transition-all duration-1000" 
              style={{ width: `${signal.patternConviction * 100}%` }}
            />
          </div>
        </div>
      </div>

      <div className="mt-4 pt-4 border-t border-gray-50 dark:border-gray-700">
        <p className="text-[11px] text-gray-400 italic">
          Last updated: {new Date(signal.timestamp).toLocaleTimeString()}
        </p>
      </div>
    </div>
  );
}
