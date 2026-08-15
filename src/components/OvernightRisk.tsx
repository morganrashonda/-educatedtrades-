import { useEffect, useState } from "react";
import { Moon, Loader2 } from "lucide-react";
import { fetchOvernightRisk } from "../server/actions";

export function OvernightRisk() {
  const [data, setData] = useState<any | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      const result = await fetchOvernightRisk();
      setData(result);
      setLoading(false);
    };
    fetchData();
  }, []);

  return (
    <div className="card p-5 transition-all duration-300 border-trade-600/20">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-lg bg-indigo-500/10 flex items-center justify-center">
            <Moon className="w-5 h-5 text-indigo-400" />
          </div>
          <div>
            <h3 className="font-bold text-sm text-trade-100">Overnight Risk</h3>
            <span className="text-[10px] text-trade-400">Gap exposure snapshot</span>
          </div>
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-10">
          <Loader2 className="w-5 h-5 animate-spin text-trade-400" />
        </div>
      ) : data ? (
        <div className="space-y-2">
          {Object.entries(data).map(([key, value]) => (
            <div key={key} className="flex justify-between py-2.5 border-b border-trade-600/20 last:border-0">
              <span className="text-xs text-trade-400 capitalize">{key.replace(/_/g, ' ')}</span>
              <span className="font-mono font-bold text-sm text-trade-100">{String(value)}</span>
            </div>
          ))}
        </div>
      ) : (
        <div className="py-8 text-center">
          <div className="w-12 h-12 rounded-xl bg-trade-700/30 flex items-center justify-center mx-auto mb-4">
            <Moon className="w-6 h-6 text-trade-400" />
          </div>
          <p className="text-xs text-trade-400 mb-3">
            Overnight risk data will appear here once the risk snapshot is available.
          </p>
          <div className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-trade-700/50 text-[10px] font-semibold text-trade-400 uppercase tracking-wider">
            <span className="w-1.5 h-1.5 rounded-full bg-amber-bright" />
            Coming Soon
          </div>
        </div>
      )}
    </div>
  );
}