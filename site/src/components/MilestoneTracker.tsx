import type { Milestone } from '../types';
import { Target, TrendingUp } from 'lucide-react';

export function MilestoneTracker({ milestones }: { milestones: Milestone[] }) {
  const milestone = milestones?.[0] || { target: 1000, current: 0, percent: 0, remaining: 1000 };

  return (
    <div className="bg-white dark:bg-gray-900 rounded-2xl p-6 border border-gray-100 dark:border-gray-800 shadow-sm">
      <div className="flex items-center justify-between mb-6">
        <h3 className="font-bold flex items-center gap-2">
          <Target className="w-5 h-5 text-indigo-500" />
          Milestone Tracker
        </h3>
        <span className="text-xs font-bold text-indigo-500 bg-indigo-50 dark:bg-indigo-900/20 px-2 py-1 rounded-full uppercase">
          Target: ${milestone.target.toLocaleString()}
        </span>
      </div>

      <div className="space-y-4">
        <div className="flex justify-between text-sm">
          <span className="text-gray-500">Current Profit</span>
          <span className={`font-bold ${milestone.current >= 0 ? 'text-green-500' : 'text-red-500'}`}>
            ${milestone.current.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </span>
        </div>

        <div className="relative pt-1">
          <div className="overflow-hidden h-3 mb-4 text-xs flex rounded-full bg-gray-100 dark:bg-gray-800">
            <div 
              style={{ width: `${Math.min(100, Math.max(0, milestone.percent))}%` }}
              className="shadow-none flex flex-col text-center whitespace-nowrap text-white justify-center bg-indigo-500 transition-all duration-500"
            ></div>
          </div>
        </div>

        <div className="flex justify-between items-end">
          <div>
            <div className="text-[10px] uppercase font-bold text-gray-400 mb-1">Progress</div>
            <div className="text-xl font-bold">{Math.round(milestone.percent)}%</div>
          </div>
          <div className="text-right">
            <div className="text-[10px] uppercase font-bold text-gray-400 mb-1">Remaining</div>
            <div className="text-sm font-bold text-gray-600 dark:text-gray-300">
              ${milestone.remaining.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            </div>
          </div>
        </div>
        
        <div className="mt-4 pt-4 border-t border-gray-50 dark:border-gray-800">
          <p className="text-[11px] text-gray-400 italic">
            Next Level: <span className="text-indigo-400 font-medium">Sniper Tier (Live Market API)</span> unlocked at $1,000 profit.
          </p>
        </div>
      </div>
    </div>
  );
}
