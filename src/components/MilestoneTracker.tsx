import type { Milestone } from '../types';
import { Target, TrendingUp, Award, Rocket } from 'lucide-react';

export function MilestoneTracker({ milestones }: { milestones: Milestone[] }) {
  const milestone = milestones?.[0] || { target: 1000, current: 0, percent: 0, remaining: 1000 };
  const isReached = milestone.percent >= 100;

  return (
    <div className="card p-6">
      <div className="flex items-center justify-between mb-5">
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-lg bg-accent-500/10 flex items-center justify-center">
            <Target className="w-5 h-5 text-accent-400" />
          </div>
          <div>
            <h3 className="font-bold text-sm text-trade-100">Milestone</h3>
            <span className="text-[10px] text-trade-400">Profit target tracker</span>
          </div>
        </div>
        <span className="badge-indigo text-[9px]">
          ${milestone.target.toLocaleString()}
        </span>
      </div>

      <div className="space-y-5">
        <div className="flex justify-between items-end">
          <div>
            <span className="metric-label">Current Profit</span>
            <div className={`text-2xl font-bold font-mono mt-1 ${milestone.current >= 0 ? 'text-green-bright' : 'text-red-bright'}`}>
              ${milestone.current.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            </div>
          </div>
          {isReached && (
            <div className="flex items-center gap-1.5 badge-green text-[9px]">
              <Award className="w-3 h-3" />
              REACHED
            </div>
          )}
        </div>

        <div className="space-y-2">
          <div className="flex justify-between text-xs">
            <span className="text-trade-400">Progress</span>
            <span className="font-bold font-mono text-trade-100">{Math.round(milestone.percent)}%</span>
          </div>
          <div className="progress-bar h-3">
            <div 
              className={`progress-bar-fill ${isReached ? 'bg-gradient-to-r from-green-500 to-green-bright' : 'bg-gradient-to-r from-accent-500 to-accent-400'}`}
              style={{ width: `${Math.min(100, Math.max(0, milestone.percent))}%` }}
            />
          </div>
        </div>

        <div className="flex justify-between items-center p-3 rounded-lg bg-trade-700/30">
          <div className="text-[10px] uppercase font-bold text-trade-400">Remaining</div>
          <div className="text-sm font-bold font-mono text-trade-100">
            ${milestone.remaining.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </div>
        </div>
        
        <div className="pt-3 divider">
          <div className="flex items-center gap-2">
            <Rocket className="w-3.5 h-3.5 text-accent-400" />
            <p className="text-[10px] text-trade-400">
              <span className="text-accent-400 font-semibold">Sniper Tier</span> unlocked at $1,000 profit
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}