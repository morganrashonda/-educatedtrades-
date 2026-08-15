import { useEffect, useState, useRef } from "react";
import { fetchHeartbeat } from "../server/actions";
import { Heart, Activity, Clock, Zap, Bot } from "lucide-react";

type HeartbeatData = {
  timestamp: number;
  datetime_utc: string;
  cycle_number: number;
  phase: string;
  mode: string;
  status: string;
};

export function HeartbeatStatus() {
  const [heartbeat, setHeartbeat] = useState<HeartbeatData | null>(null);
  const [isAlive, setIsAlive] = useState(false);
  const [lastSeen, setLastSeen] = useState<string | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    // Named `poll`, deliberately not `fetchHeartbeat`. The imported server
    // function is called `fetchHeartbeat`; a local of the same name shadows it
    // and turns the call below into unbounded recursion, which the catch
    // swallows into a permanent "not alive". Caught by tsc, not by review.
    const poll = async () => {
      try {
        const data = await fetchHeartbeat();
        if (data && data.status === 'alive') {
          setHeartbeat(data);
          setIsAlive(true);
          setLastSeen(new Date().toLocaleTimeString());
        } else {
          setIsAlive(false);
        }
      } catch {
        setIsAlive(false);
      }
    };

    poll();

    intervalRef.current = setInterval(poll, 15000);

    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, []);

  const timeSinceLastBeat = heartbeat
    ? new Date(heartbeat.timestamp * 1000).toLocaleTimeString()
    : '--';

  return (
    <div className="card p-5 transition-all duration-300 border-trade-600/20">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-lg bg-pink-500/10 flex items-center justify-center">
            <Heart className="w-5 h-5 text-pink-400" />
          </div>
          <div>
            <h3 className="font-bold text-sm text-trade-100">Bot Heartbeat</h3>
            <span className="text-[10px] text-trade-400">System health monitor</span>
          </div>
        </div>

        <div className={`flex items-center gap-1.5 px-3 py-1 rounded-full text-[10px] font-bold uppercase tracking-wider border ${
          isAlive
            ? 'bg-green-bright/10 text-green-bright border-green-bright/20'
            : 'bg-red-bright/10 text-red-bright border-red-bright/20'
        }`}>
          <span className={`w-1.5 h-1.5 rounded-full ${isAlive ? 'bg-green-bright animate-pulse' : 'bg-red-bright'}`} />
          {isAlive ? 'ALIVE' : 'OFFLINE'}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="p-3 rounded-lg bg-trade-700/30">
          <div className="flex items-center gap-1.5 text-[10px] text-trade-400 mb-1.5">
            <Zap className="w-3 h-3" />
            <span>Cycle</span>
          </div>
          <span className="font-mono font-bold text-sm text-trade-100">
            #{heartbeat?.cycle_number ?? '--'}
          </span>
        </div>

        <div className="p-3 rounded-lg bg-trade-700/30">
          <div className="flex items-center gap-1.5 text-[10px] text-trade-400 mb-1.5">
            <Activity className="w-3 h-3" />
            <span>Phase</span>
          </div>
          <span className="font-mono font-bold text-sm capitalize text-trade-100">
            {heartbeat?.phase ?? '--'}
          </span>
        </div>

        <div className="p-3 rounded-lg bg-trade-700/30">
          <div className="flex items-center gap-1.5 text-[10px] text-trade-400 mb-1.5">
            <Clock className="w-3 h-3" />
            <span>Last Beat</span>
          </div>
          <span className="font-mono font-bold text-xs text-trade-100">
            {timeSinceLastBeat}
          </span>
        </div>

        <div className="p-3 rounded-lg bg-trade-700/30">
          <div className="flex items-center gap-1.5 text-[10px] text-trade-400 mb-1.5">
            <Bot className="w-3 h-3" />
            <span>Mode</span>
          </div>
          <span className={`font-mono font-bold text-xs capitalize ${
            heartbeat?.mode === 'autonomous' ? 'text-accent-400' : 'text-amber-bright'
          }`}>
            {heartbeat?.mode ?? '--'}
          </span>
        </div>
      </div>

      {lastSeen && (
        <div className="mt-4 pt-3 divider flex items-center justify-between text-[10px] text-trade-500">
          <span>Last verified</span>
          <span className="font-mono">{lastSeen}</span>
        </div>
      )}
    </div>
  );
}