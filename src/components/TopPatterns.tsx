import type { PatternSummary } from '../types';
import { Award, Zap, ShieldCheck, BarChart3 } from 'lucide-react';

export function TopPatterns({ patterns }: { patterns: PatternSummary[] }) {
  return (
    <div className="card p-6">
      <div className="flex items-center justify-between mb-5">
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-lg bg-amber-bright/10 flex items-center justify-center">
            <Award className="w-5 h-5 text-amber-bright" />
          </div>
          <div>
            <h3 className="font-bold text-sm text-trade-100">Top Patterns</h3>
            <span className="text-[10px] text-trade-400">Best performing signals</span>
          </div>
        </div>
      </div>

      <div className="space-y-3">
        {patterns.length === 0 ? (
          <div className="py-10 text-center">
            <div className="w-12 h-12 rounded-xl bg-trade-700/30 flex items-center justify-center mx-auto mb-4">
              <BarChart3 className="w-6 h-6 text-trade-400" />
            </div>
            <p className="text-sm text-trade-400 italic">Gathering more data...</p>
          </div>
        ) : (
          patterns.slice(0, 4).map((p) => (
            <div key={p.signature} className="flex items-center justify-between p-3 rounded-xl bg-trade-700/30 border border-trade-600/20 transition-all duration-200 hover:bg-trade-700/50 hover:border-trade-500/30">
              <div className="flex items-center gap-3 min-w-0">
                <div className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 ${
                  p.is_robust ? 'bg-green-bright/10' : 'bg-amber-bright/10'
                }`}>
                  {p.is_robust ? (
                    <ShieldCheck className="w-4.5 h-4.5 text-green-bright" />
                  ) : (
                    <Zap className="w-4.5 h-4.5 text-amber-bright" />
                  )}
                </div>
                <div className="min-w-0">
                  <div className="text-[10px] font-mono text-trade-400 truncate max-w-[120px] uppercase tracking-tight">
                    {p.signature.replace(/_/g, ' ')}
                  </div>
                  <div className="text-xs font-semibold text-trade-300 mt-0.5">
                    {p.count} Occurrences
                  </div>
                </div>
              </div>
              <div className="text-right shrink-0">
                <div className="text-sm font-bold font-mono text-green-bright">
                  {Math.round(p.win_rate * 100)}%
                </div>
                <div className="text-[9px] font-bold text-trade-500 uppercase tracking-wider">
                  Win Rate
                </div>
              </div>
            </div>
          ))
        )}
      </div>
      
      {patterns.length > 0 && (
        <div className="mt-5 pt-4 divider">
          <button className="w-full py-2.5 text-xs font-bold text-accent-400 hover:text-accent-300 transition-colors uppercase tracking-wider rounded-lg hover:bg-accent-500/5">
            View All Pattern Stats
          </button>
        </div>
      )}
    </div>
  );
}