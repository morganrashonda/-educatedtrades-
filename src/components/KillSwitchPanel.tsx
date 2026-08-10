import { useState } from "react";
import { AlertTriangle, Shield, ShieldOff, RotateCcw, Zap } from "lucide-react";
import { triggerKillSwitch, resetKillSwitch } from "../server/api";

export function KillSwitchPanel({ killSwitchActive, onStatusChange }: { killSwitchActive: boolean; onStatusChange?: (active: boolean) => void }) {
  const [loading, setLoading] = useState<'kill' | 'reset' | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleKill = async () => {
    setLoading('kill');
    setError(null);
    try {
      const result = await triggerKillSwitch();
      if (result) {
        onStatusChange?.(true);
      } else {
        setError('Failed to activate kill switch');
      }
    } catch {
      setError('Network error');
    } finally {
      setLoading(null);
    }
  };

  const handleReset = async () => {
    setLoading('reset');
    setError(null);
    try {
      const result = await resetKillSwitch();
      if (result) {
        onStatusChange?.(false);
      } else {
        setError('Failed to reset kill switch');
      }
    } catch {
      setError('Network error');
    } finally {
      setLoading(null);
    }
  };

  return (
    <div className={`card p-5 transition-all duration-300 ${
      killSwitchActive
        ? 'card-border-glow border-red-bright/30 glow-red'
        : 'border-trade-600/20'
    }`}>
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2.5">
          <div className={`w-9 h-9 rounded-lg flex items-center justify-center ${
            killSwitchActive
              ? 'bg-red-bright/10'
              : 'bg-green-bright/10'
          }`}>
            {killSwitchActive
              ? <ShieldOff className="w-5 h-5 text-red-bright" />
              : <Shield className="w-5 h-5 text-green-bright" />
            }
          </div>
          <div>
            <h3 className="font-bold text-sm text-trade-100">Kill Switch</h3>
            <span className="text-[10px] text-trade-400">Emergency stop</span>
          </div>
        </div>
        <div className={`flex items-center gap-1.5 px-3 py-1 rounded-full text-[10px] font-bold uppercase tracking-wider border ${
          killSwitchActive
            ? 'bg-red-bright/10 text-red-bright border-red-bright/20 animate-flash-danger'
            : 'bg-green-bright/10 text-green-bright border-green-bright/20'
        }`}>
          <span className={`w-1.5 h-1.5 rounded-full ${killSwitchActive ? 'bg-red-bright' : 'bg-green-bright'}`} />
          {killSwitchActive ? 'KILLED' : 'ARMED'}
        </div>
      </div>

      {/* Status message when active */}
      {killSwitchActive && (
        <div className="mb-4 p-3 rounded-lg bg-red-bright/5 border border-red-bright/20">
          <div className="flex items-start gap-2.5">
            <AlertTriangle className="w-4.5 h-4.5 text-red-bright shrink-0 mt-0.5" />
            <div>
              <p className="text-xs font-semibold text-red-bright">Trading KILLED</p>
              <p className="text-[11px] text-trade-400 mt-0.5">
                All automated operations halted. Press "Reset Kill" to re-enable.
              </p>
            </div>
          </div>
        </div>
      )}

      {error && (
        <div className="mb-3 text-xs text-red-bright font-medium bg-red-bright/5 px-3 py-2 rounded-lg">{error}</div>
      )}

      {/* Action buttons */}
      <div className="flex gap-3">
        <button
          onClick={handleKill}
          disabled={loading === 'kill' || killSwitchActive}
          className={`flex-1 flex items-center justify-center gap-2 px-4 py-3 rounded-xl font-bold text-xs transition-all duration-200 ${
            killSwitchActive
              ? 'bg-trade-700/50 text-trade-500 cursor-not-allowed'
              : 'bg-red-bright/90 text-white hover:bg-red-bright active:bg-red-deep shadow-lg shadow-red-bright/20 hover:shadow-red-bright/30'
          } disabled:opacity-50`}
        >
          <Zap className={`w-4 h-4 ${loading === 'kill' ? 'animate-ping' : ''}`} />
          EMERGENCY KILL
        </button>

        <button
          onClick={handleReset}
          disabled={loading === 'reset' || !killSwitchActive}
          className={`flex items-center justify-center gap-2 px-4 py-3 rounded-xl font-bold text-xs transition-all duration-200 ${
            !killSwitchActive
              ? 'bg-trade-700/50 text-trade-500 cursor-not-allowed'
              : 'bg-amber-bright/90 text-white hover:bg-amber-bright active:bg-amber-600 shadow-lg shadow-amber-bright/20 hover:shadow-amber-bright/30'
          } disabled:opacity-50`}
        >
          <RotateCcw className={`w-3.5 h-3.5 ${loading === 'reset' ? 'animate-spin' : ''}`} />
          Reset Kill
        </button>
      </div>
    </div>
  );
}