import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { createServerFn } from "@tanstack/react-start";
import { readFile } from "node:fs/promises";
import { useState, useEffect, useCallback, useRef } from "react";
import { PortfolioCard } from "../components/PortfolioCard";
import { SignalCard } from "../components/SignalCard";
import { HistoryTable } from "../components/HistoryTable";
import { PerformanceChart } from "../components/PerformanceChart";
import { MilestoneTracker } from "../components/MilestoneTracker";
import { TopPatterns } from "../components/TopPatterns";
import { 
  getDashboardData as getRealDashboardData, 
  setOrchestratorMode
} from "../server/api";
import { 
  LayoutDashboard, 
  Settings, 
  Bell, 
  Search, 
  User, 
  Activity, 
  Zap, 
  X,
  CheckCircle2,
  AlertCircle,
  TrendingUp,
  Award
} from "lucide-react";
import type { Trade, Alert } from "../types";

const getDashboardData = createServerFn({ method: "GET" }).handler(async () => {
  let businessName = "Educated Trades";
  try {
    const cfg = JSON.parse(await readFile("site.json", "utf8")) as {
      businessName?: string;
    };
    businessName = cfg.businessName?.trim() ?? businessName;
  } catch {
    // Fallback to default
  }

  const realData = await getRealDashboardData();

  return {
    businessName,
    ...realData
  };
});

const toggleMode = createServerFn({ method: "POST" })
  .validator((d: { mode: 'manual' | 'autonomous' }) => d)
  .handler(async ({ data }) => {
    return await setOrchestratorMode(data.mode);
  });

export const Route = createFileRoute("/")({
  loader: () => getDashboardData(),
  component: Dashboard,
});

function Dashboard() {
  const initialData = Route.useLoaderData();
  const [data, setData] = useState(initialData);
  const [isNotificationsOpen, setIsNotificationsOpen] = useState(false);
  const [notifications, setNotifications] = useState<any[]>([]);
  const [isDeploying, setIsDeploying] = useState(false);
  
  // Refs for tracking seen items across poll intervals
  const lastTradeCountRef = useRef(initialData.trades.length);
  const seenAlertIdsRef = useRef(new Set(initialData.alerts.map((a: Alert) => a.id)));

  // Poll for updates
  useEffect(() => {
    const interval = setInterval(async () => {
      try {
        const freshData = await getDashboardData();
        
        // Use functional update to ensure we have latest data
        setData(freshData);

        // Check for new trades or alerts
        const newTradeCount = freshData.trades.length;
        const newAlerts = freshData.alerts.filter((a: Alert) => !seenAlertIdsRef.current.has(a.id));

        if (newTradeCount > lastTradeCountRef.current || newAlerts.length > 0) {
          const tradeNotifs = freshData.trades
            .slice(0, Math.max(0, newTradeCount - lastTradeCountRef.current))
            .map((t: Trade) => ({
              id: `trade-${t.id}-${Date.now()}`,
              type: 'trade',
              title: `Trade Executed: ${t.symbol}`,
              description: `${t.side.toUpperCase()} @ ${t.entryPrice}`,
              timestamp: new Date().toLocaleTimeString(),
              conviction: freshData.signals.find((s: any) => s.symbol === 'MARKET')?.sentiment || 0,
              severity: 'info'
            }));

          const alertNotifs = newAlerts.map((a: any) => ({
            id: `alert-${a.id}`,
            type: 'alert',
            title: `System Alert: ${a.type}`,
            description: a.message,
            timestamp: a.created_at ? new Date(a.created_at.replace(' ', 'T')).toLocaleTimeString() : new Date().toLocaleTimeString(),
            conviction: 0,
            severity: a.severity
          }));

          setNotifications(prev => [...tradeNotifs, ...alertNotifs, ...prev]);
          
          // Update refs
          lastTradeCountRef.current = newTradeCount;
          newAlerts.forEach((a: Alert) => seenAlertIdsRef.current.add(a.id));
          
          // Auto-open sidebar if new high-severity alert or trade
          if (tradeNotifs.length > 0 || alertNotifs.some((n: any) => n.severity === 'critical' || n.severity === 'error')) {
            setIsNotificationsOpen(true);
          }
        }
      } catch (err) {
        console.error("Polling error:", err);
      }
    }, 5000); // Poll every 5 seconds

    return () => clearInterval(interval);
  }, []);

  const handleDeploySniper = async () => {
    setIsDeploying(true);
    const newMode = data.systemHealth.mode === 'autonomous' ? 'manual' : 'autonomous';
    try {
      await toggleMode({ data: { mode: newMode } });
      // Refresh data immediately
      const freshData = await getDashboardData();
      setData(freshData);
    } catch (err) {
      console.error("Failed to toggle mode:", err);
    } finally {
      setIsDeploying(false);
    }
  };

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-950 text-gray-900 dark:text-gray-100 flex relative overflow-hidden">
      {/* Sidebar */}
      <aside className="w-64 bg-white dark:bg-gray-900 border-r border-gray-200 dark:border-gray-800 hidden lg:flex flex-col">
        <div className="p-6">
          <h1 className="text-xl font-bold text-indigo-600 dark:text-indigo-400 flex items-center gap-2">
            <LayoutDashboard className="w-6 h-6" />
            {data.businessName}
          </h1>
        </div>
        <nav className="flex-1 px-4 space-y-2 mt-4">
          <a href="#" className="flex items-center gap-3 px-4 py-3 bg-indigo-50 dark:bg-indigo-900/20 text-indigo-600 dark:text-indigo-400 rounded-xl font-semibold">
            <LayoutDashboard className="w-5 h-5" />
            Dashboard
          </a>
          <a href="#" className="flex items-center gap-3 px-4 py-3 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl transition-colors">
            <Search className="w-5 h-5" />
            Market Explorer
          </a>
          <a href="#" className="flex items-center gap-3 px-4 py-3 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl transition-colors">
            <Bell className="w-5 h-5" />
            Alerts
          </a>
          <a href="#" className="flex items-center gap-3 px-4 py-3 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl transition-colors">
            <Settings className="w-5 h-5" />
            Settings
          </a>
        </nav>
        <div className="p-4 border-t border-gray-100 dark:border-gray-800">
          <div className="flex items-center gap-3 px-4 py-3">
            <div className="w-8 h-8 rounded-full bg-indigo-100 dark:bg-indigo-900 flex items-center justify-center text-indigo-600 dark:text-indigo-400">
              <User className="w-5 h-5" />
            </div>
            <div>
              <div className="text-sm font-bold">Frontend Dev</div>
              <div className="text-xs text-gray-500">Premium Tier</div>
            </div>
          </div>
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 overflow-y-auto">
        <header className="h-16 bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-800 flex items-center justify-between px-8 sticky top-0 z-10">
          <div className="font-bold text-lg lg:hidden text-indigo-600">
            {data.businessName}
          </div>
          
          <div className="hidden lg:flex items-center gap-6 text-sm font-medium">
            <div className="flex items-center gap-2">
              <span className="text-gray-400 italic">System Status:</span>
              <div className="flex items-center gap-1.5 px-2 py-1 rounded-md bg-green-50 dark:bg-green-900/10 text-green-600 dark:text-green-400 border border-green-100 dark:border-green-900/20">
                <CheckCircle2 className="w-3.5 h-3.5" />
                <span>Healthy</span>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-gray-400 italic">Mode:</span>
              <span className={`capitalize ${data.systemHealth.mode === 'autonomous' ? 'text-indigo-500' : 'text-amber-500'}`}>
                {data.systemHealth.mode}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-gray-400 italic">Cycle:</span>
              <span className="text-gray-600 dark:text-gray-300 font-mono">#{data.systemHealth.cycleCount}</span>
            </div>
          </div>

          <div className="flex items-center gap-4">
            <button 
              onClick={() => setIsNotificationsOpen(!isNotificationsOpen)}
              className="p-2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 transition-colors relative"
            >
              <Bell className="w-5 h-5" />
              {notifications.length > 0 && (
                <span className="absolute top-1.5 right-1.5 w-2 h-2 bg-red-500 rounded-full border-2 border-white dark:border-gray-900"></span>
              )}
            </button>
            <div className="h-8 w-px bg-gray-200 dark:bg-gray-800 mx-2" />
            <button 
              onClick={handleDeploySniper}
              disabled={isDeploying}
              className={`${
                data.systemHealth.mode === 'autonomous' 
                  ? 'bg-red-600 hover:bg-red-700' 
                  : 'bg-indigo-600 hover:bg-indigo-700'
              } text-white px-4 py-2 rounded-lg text-sm font-bold transition-all flex items-center gap-2 disabled:opacity-50`}
            >
              <Zap className={`w-4 h-4 ${isDeploying ? 'animate-pulse' : ''}`} />
              {isDeploying ? 'Processing...' : data.systemHealth.mode === 'autonomous' ? 'Stop Sniper' : 'Deploy Sniper'}
            </button>
          </div>
        </header>

        <div className="p-8 max-w-7xl mx-auto space-y-8">
          {/* Stats Summary Row */}
          <PortfolioCard portfolio={data.portfolio} />

          {/* Performance & Milestones Grid */}
          <div className="grid grid-cols-1 xl:grid-cols-3 gap-8">
            {/* P&L Line Chart */}
            <div className="xl:col-span-2">
              <PerformanceChart data={data.chartData} />
            </div>
            
            {/* Milestone Tracker */}
            <div className="space-y-8">
              <MilestoneTracker milestones={data.milestones} />
              <TopPatterns patterns={data.topPatterns} />
            </div>
          </div>

          {/* Signals and History Grid */}
          <div className="grid grid-cols-1 xl:grid-cols-3 gap-8">
            <div className="xl:col-span-2">
               <HistoryTable trades={data.trades} />
            </div>
            <div className="space-y-6">
              <h2 className="text-sm font-bold uppercase tracking-wider text-gray-400 px-1 flex items-center gap-2">
                <Activity className="w-4 h-4" />
                Live Signals
              </h2>
              {data.signals.length > 0 ? (
                data.signals.map(signal => (
                  <SignalCard key={signal.id} signal={signal} />
                ))
              ) : (
                <div className="p-4 bg-white dark:bg-gray-800 rounded-xl border border-gray-100 dark:border-gray-700 text-center text-gray-500">
                  No active signals
                </div>
              )}
            </div>
          </div>
        </div>
      </main>

      {/* Notifications Sidebar */}
      <div className={`fixed inset-y-0 right-0 w-80 bg-white dark:bg-gray-900 shadow-2xl border-l border-gray-200 dark:border-gray-800 transform transition-transform duration-300 ease-in-out z-50 ${isNotificationsOpen ? 'translate-x-0' : 'translate-x-full'}`}>
        <div className="p-6 h-full flex flex-col">
          <div className="flex items-center justify-between mb-8">
            <h2 className="text-lg font-bold flex items-center gap-2">
              <Bell className="w-5 h-5 text-indigo-500" />
              Notifications
            </h2>
            <button onClick={() => setIsNotificationsOpen(false)} className="p-1 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg">
              <X className="w-5 h-5" />
            </button>
          </div>

          <div className="flex-1 overflow-y-auto space-y-4 pr-2">
            {notifications.length === 0 ? (
              <div className="flex flex-col items-center justify-center h-40 text-gray-400 space-y-2">
                <Bell className="w-8 h-8 opacity-20" />
                <p className="text-sm italic">No recent activity</p>
              </div>
            ) : (
              notifications.map((notif, i) => (
                <div key={notif.id || i} className={`p-4 rounded-xl border space-y-2 ${
                  notif.type === 'alert' 
                    ? notif.severity === 'critical' || notif.severity === 'error'
                      ? 'bg-red-50 dark:bg-red-900/10 border-red-100 dark:border-red-900/20'
                      : 'bg-amber-50 dark:bg-amber-900/10 border-amber-100 dark:border-amber-900/20'
                    : 'bg-gray-50 dark:bg-gray-800/50 border-gray-100 dark:border-gray-700'
                }`}>
                  <div className="flex justify-between items-start">
                    <h3 className={`font-bold text-sm ${
                      notif.type === 'alert'
                        ? notif.severity === 'critical' || notif.severity === 'error'
                          ? 'text-red-600 dark:text-red-400'
                          : 'text-amber-600 dark:text-amber-400'
                        : ''
                    }`}>{notif.title}</h3>
                    <span className="text-[10px] text-gray-400 font-mono">{notif.timestamp}</span>
                  </div>
                  <p className="text-xs text-gray-500 dark:text-gray-400">{notif.description}</p>
                  
                  {notif.type === 'trade' && (
                    <div className="pt-1 flex items-center gap-2">
                      <span className="text-[10px] uppercase font-bold text-gray-400">Conviction:</span>
                      <div className="flex-1 h-1 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                        <div 
                          className={`h-full ${notif.conviction > 0 ? 'bg-green-500' : 'bg-red-500'}`}
                          style={{ width: `${Math.abs(notif.conviction) * 100}%` }}
                        />
                      </div>
                    </div>
                  )}

                  {notif.type === 'alert' && (
                    <div className="flex items-center gap-1.5 pt-1">
                      <AlertCircle className={`w-3 h-3 ${
                        notif.severity === 'critical' || notif.severity === 'error'
                          ? 'text-red-500'
                          : 'text-amber-500'
                      }`} />
                      <span className={`text-[10px] uppercase font-bold ${
                        notif.severity === 'critical' || notif.severity === 'error'
                          ? 'text-red-500'
                          : 'text-amber-500'
                      }`}>{notif.severity}</span>
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
          
          {notifications.length > 0 && (
            <button 
              onClick={() => setNotifications([])}
              className="mt-6 w-full py-3 text-sm text-gray-500 hover:text-indigo-500 font-semibold transition-colors"
            >
              Clear all
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
