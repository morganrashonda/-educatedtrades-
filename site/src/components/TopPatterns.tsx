import type { PatternSummary } from '../types';
import { Award, Zap, ShieldCheck } from 'lucide-react';

export function TopPatterns({ patterns }: { patterns: PatternSummary[] }) {
  return (
    <div className="bg-white dark:bg-gray-900 rounded-2xl p-6 border border-gray-100 dark:border-gray-800 shadow-sm h-full">
      <div className="flex items-center justify-between mb-6">
        <h3 className="font-bold flex items-center gap-2">
          <Award className="w-5 h-5 text-amber-500" />
          Top Performing Patterns
        </h3>
      </div>

      <div className="space-y-4">
        {patterns.length === 0 ? (
          <div className="py-8 text-center text-gray-400 text-sm italic">
            Gathering more data...
          </div>
        ) : (
          patterns.slice(0, 4).map((p, i) => (
            <div key={p.signature} className="flex items-center justify-between p-3 rounded-xl bg-gray-50 dark:bg-gray-800/50 border border-gray-100 dark:border-gray-800">
              <div className="flex items-center gap-3">
                <div className={`w-8 h-8 rounded-lg flex items-center justify-center ${
                  p.is_robust ? 'bg-green-100 text-green-600' : 'bg-amber-100 text-amber-600'
                }`}>
                  {p.is_robust ? <ShieldCheck className="w-4 h-4" /> : <Zap className="w-4 h-4" />}
                </div>
                <div>
                  <div className="text-[10px] font-mono text-gray-400 truncate w-32 uppercase tracking-tight">
                    {p.signature.replace(/_/g, ' ')}
                  </div>
                  <div className="text-xs font-bold truncate w-32">
                    {p.count} Occurrences
                  </div>
                </div>
              </div>
              <div className="text-right">
                <div className="text-sm font-bold text-green-500">
                  {Math.round(p.win_rate * 100)}% Win
                </div>
                <div className="text-[10px] font-bold text-gray-400 uppercase">
                  Win Rate
                </div>
              </div>
            </div>
          ))
        )}
      </div>
      
      <div className="mt-6 pt-4 border-t border-gray-50 dark:border-gray-800">
        <button className="w-full py-2 text-xs font-bold text-indigo-500 hover:text-indigo-600 transition-colors uppercase tracking-wider">
          View All Pattern Stats
        </button>
      </div>
    </div>
  );
}
