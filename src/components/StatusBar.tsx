import { useEffect, useState, useRef } from "react";
import { Wifi, WifiOff, Clock, Database, RefreshCw } from "lucide-react";

type StatusBarProps = {
  lastUpdate?: string;
  apiConnected?: boolean;
  cycleCount?: number;
};

export function StatusBar({ lastUpdate, apiConnected, cycleCount }: StatusBarProps) {
  const [serverTime, setServerTime] = useState(new Date().toLocaleTimeString());
  // Latency was never wired up: the setter was never called, so this stayed
  // null and the readout below never rendered. Kept as an explicit constant
  // rather than deleted, so the intent survives until it is measured for real.
  const latencyMs: number | null = null;
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    // Update server time every second
    intervalRef.current = setInterval(() => {
      setServerTime(new Date().toLocaleTimeString());
    }, 1000);

    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, []);

  return (
    <div className="status-bar">
      <div className="max-w-7xl mx-auto flex items-center h-7 px-4 lg:px-8 overflow-x-auto">
        {/* Connection Status */}
        <div className="status-bar-item">
          {apiConnected !== false ? (
            <>
              <Wifi className="w-3 h-3 text-green-bright" />
              <span className="text-green-bright font-semibold">Connected</span>
            </>
          ) : (
            <>
              <WifiOff className="w-3 h-3 text-red-bright" />
              <span className="text-red-bright font-semibold">Disconnected</span>
            </>
          )}
        </div>

        {/* Latency */}
        {latencyMs !== null && (
          <div className="status-bar-item">
            <RefreshCw className="w-3 h-3" />
            <span>{latencyMs}ms</span>
          </div>
        )}

        {/* Server Time */}
        <div className="status-bar-item">
          <Clock className="w-3 h-3" />
          <span>{serverTime} UTC</span>
        </div>

        {/* Cycle */}
        {cycleCount !== undefined && (
          <div className="status-bar-item">
            <Database className="w-3 h-3" />
            <span>Cycle #{cycleCount}</span>
          </div>
        )}

        {/* Last Update */}
        {lastUpdate && (
          <div className="status-bar-item">
            <span>Last update: {lastUpdate}</span>
          </div>
        )}

        {/* Spacer */}
        <div className="flex-1" />

        {/* Version info */}
        <div className="status-bar-item border-r-0">
          <span className="text-trade-500">Educated Trades v3.0</span>
        </div>
      </div>
    </div>
  );
}