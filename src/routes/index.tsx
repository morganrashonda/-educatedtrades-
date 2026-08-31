import { createFileRoute } from "@tanstack/react-router";
import { createServerFn } from "@tanstack/react-start";
import { readFile } from "node:fs/promises";
import { useState, useEffect, useRef } from "react";
import { PortfolioCard } from "../components/PortfolioCard";
import { SignalCard } from "../components/SignalCard";
import { HistoryTable } from "../components/HistoryTable";
import { PerformanceChart } from "../components/PerformanceChart";
import { MilestoneTracker } from "../components/MilestoneTracker";
import { TopPatterns } from "../components/TopPatterns";
import { KillSwitchPanel } from "../components/KillSwitchPanel";
import { DailyLossTracker } from "../components/DailyLossTracker";
import { HeartbeatStatus } from "../components/HeartbeatStatus";
import { OvernightRisk } from "../components/OvernightRisk";
import { DrawdownDisplay } from "../components/DrawdownDisplay";
import { QuickStats } from "../components/QuickStats";
import { StatusBar } from "../components/StatusBar";
import { SkeletonCard, SkeletonTable, SkeletonChart } from "../components/SkeletonCard";
// Server-only: this module holds the bot API token and throws if evaluated in
// a browser. Both symbols below are used exclusively inside createServerFn
// handlers, which run on the server. triggerKillSwitch/resetKillSwitch were
// imported here and never used -- dead imports that still pulled a
// token-bearing module into the client graph. The kill switch reaches the API
// through ../server/actions instead.
import {
  getDashboardData as getRealDashboardData,
  setOrchestratorMode,
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
  AlertCircle,
  TrendingUp,
  Shield,
  BarChart3,
  Menu,
  BookOpen,
  Signal,
  Bot,
  LogOut,
  Settings2,
  HelpCircle,
  ChevronDown,
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
  const [isLoading, setIsLoading] = useState(true);
  const [isNotificationsOpen, setIsNotificationsOpen] = useState(false);
  const [notifications, setNotifications] = useState<any[]>([]);
  const [isDeploying, setIsDeploying] = useState(false);
  const [peakEquity, setPeakEquity] = useState(initialData.equity);
  const [killSwitchActive, setKillSwitchActive] = useState(initialData.killSwitchActive);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [bellShake, setBellShake] = useState(false);
  const [profileMenuOpen, setProfileMenuOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [lastUpdateTime, setLastUpdateTime] = useState<string>("--");
  
  // Refs for tracking seen items across poll intervals
  const lastTradeCountRef = useRef(initialData.trades.length);
  const seenAlertIdsRef = useRef(new Set(initialData.alerts.map((a: Alert) => a.id)));
  const peakEquityRef = useRef(initialData.equity);
  const profileMenuRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);

  // Simulate initial loading
  useEffect(() => {
    const timer = setTimeout(() => setIsLoading(false), 600);
    return () => clearTimeout(timer);
  }, []);

  // Trigger bell shake animation
  const triggerBellShake = () => {
    setBellShake(true);
    setTimeout(() => setBellShake(false), 500);
  };

  // Close profile menu on outside click
  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (profileMenuRef.current && !profileMenuRef.current.contains(e.target as Node)) {
        setProfileMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  // Keyboard shortcut: Ctrl+K or Cmd+K for search
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        searchInputRef.current?.focus();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, []);

  // Poll for updates
  useEffect(() => {
    const interval = setInterval(async () => {
      try {
        const freshData = await getDashboardData();
        
        // Use functional update to ensure we have latest data
        setData(freshData);
        setKillSwitchActive(freshData.killSwitchActive);
        setLastUpdateTime(new Date().toLocaleTimeString());

        // Track peak equity for drawdown calculation
        if (freshData.equity > peakEquityRef.current) {
          peakEquityRef.current = freshData.equity;
          setPeakEquity(freshData.equity);
        }

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
            triggerBellShake();
          }
        }

        // Check for daily loss hit — auto-open notification
        if (freshData.dailyLossHit && !data.dailyLossHit) {
          setNotifications(prev => [{
            id: `loss-hit-${Date.now()}`,
            type: 'alert',
            title: '⚠️ DAILY LOSS LIMIT REACHED',
            description: `Trading halted — daily loss of ${freshData.dailyPnlPct.toFixed(2)}% exceeded the ${freshData.dailyLossLimit}% limit.`,
            timestamp: new Date().toLocaleTimeString(),
            conviction: 0,
            severity: 'critical'
          }, ...prev]);
          setIsNotificationsOpen(true);
          triggerBellShake();
        }

        // Check for kill switch state change — notify
        if (freshData.killSwitchActive && !data.killSwitchActive) {
          setNotifications(prev => [{
            id: `kill-${Date.now()}`,
            type: 'alert',
            title: '🔴 KILL SWITCH ACTIVATED',
            description: 'Emergency kill switch has been triggered. All trading operations halted.',
            timestamp: new Date().toLocaleTimeString(),
            conviction: 0,
            severity: 'critical'
          }, ...prev]);
          setIsNotificationsOpen(true);
          triggerBellShake();
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

  const isHealthy = !killSwitchActive && !data.dailyLossHit;
  const notificationCount = notifications.length;
  const hasUnreadNotifications = notificationCount > 0;

  // Compute quick stats from data
  const totalTrades = data.trades.length;
  const tradesToday = data.trades.filter((t: Trade) => {
    const today = new Date();
    const tradeDate = new Date(t.timestamp);
    return tradeDate.toDateString() === today.toDateString();
  }).length;
  const totalPnl = data.trades.reduce((sum: number, t: Trade) => sum + (t.pnl || 0), 0);
  const avgProfit = totalTrades > 0 ? totalPnl / totalTrades : 0;
  const patternCount = data.topPatterns.length;

  return (
    <div className="min-h-screen bg-trade-900 text-trade-100 flex flex-col relative">
      {/* Background gradients */}
      <div className="fixed inset-0 pointer-events-none">
        <div className="absolute top-0 left-1/4 w-96 h-96 bg-accent-500/5 rounded-full blur-[100px]" />
        <div className="absolute bottom-0 right-1/4 w-80 h-80 bg-cyan-bright/5 rounded-full blur-[100px]" />
        <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[600px] h-[600px] bg-green-bright/3 rounded-full blur-[120px]" />
      </div>

      {/* Sidebar Overlay (mobile) */}
      {sidebarOpen && (
        <div 
          className="fixed inset-0 bg-black/60 z-40 lg:hidden backdrop-blur-sm"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Main flex container for sidebar + content */}
      <div className="flex flex-1 overflow-hidden">
      <aside className={`fixed lg:static inset-y-0 left-0 z-50 w-64 bg-trade-800/95 border-r border-trade-600/20 flex flex-col transition-transform duration-300 lg:translate-x-0 ${
        sidebarOpen ? 'translate-x-0' : '-translate-x-full'
      }`}>
        {/* Logo */}
        <div className="p-6 border-b border-trade-600/20">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-accent-500 to-accent-700 flex items-center justify-center shadow-lg shadow-accent-500/20">
              <Bot className="w-5 h-5 text-white" />
            </div>
            <div>
              <h1 className="text-base font-bold text-white tracking-tight">{data.businessName}</h1>
              <div className="text-[10px] text-accent-400 font-medium uppercase tracking-wider">Trading System</div>
            </div>
          </div>
        </div>

        {/* Navigation */}
        <nav className="flex-1 px-3 py-4 space-y-1">
          <a href="#" className="nav-item active">
            <LayoutDashboard className="w-4.5 h-4.5" />
            <span>Dashboard</span>
          </a>
          <a href="#" className="nav-item">
            <Search className="w-4.5 h-4.5" />
            <span>Market Explorer</span>
          </a>
          <a href="#" className="nav-item">
            <Signal className="w-4.5 h-4.5" />
            <span>Signals</span>
          </a>
          <a href="#" className="nav-item">
            <BookOpen className="w-4.5 h-4.5" />
            <span>Trade Journal</span>
          </a>
          <a href="#" className="nav-item">
            <BarChart3 className="w-4.5 h-4.5" />
            <span>Analytics</span>
          </a>
          <a href="#" className="nav-item">
            <Shield className={`w-4.5 h-4.5 ${killSwitchActive ? 'text-red-400' : ''}`} />
            <span>Safety Controls</span>
          </a>
          <a href="#" className="nav-item">
            <Settings className="w-4.5 h-4.5" />
            <span>Settings</span>
          </a>
        </nav>

        {/* Bottom section with profile dropdown */}
        <div className="p-4 border-t border-trade-600/20 relative" ref={profileMenuRef}>
          <button 
            onClick={() => setProfileMenuOpen(!profileMenuOpen)}
            className="w-full flex items-center gap-3 px-3 py-3 rounded-lg bg-trade-700/30 hover:bg-trade-700/50 transition-colors"
          >
            <div className="w-9 h-9 rounded-lg bg-accent-500/10 flex items-center justify-center text-accent-400">
              <User className="w-5 h-5" />
            </div>
            <div className="flex-1 min-w-0 text-left">
              <div className="text-sm font-semibold text-trade-100 truncate">Trader</div>
              <div className="text-[10px] font-medium text-accent-400 uppercase tracking-wider">Premium Tier</div>
            </div>
            <ChevronDown className={`w-3.5 h-3.5 text-trade-400 transition-transform duration-200 ${profileMenuOpen ? 'rotate-180' : ''}`} />
          </button>

          {/* Profile Dropdown */}
          <div className={`dropdown-menu ${profileMenuOpen ? 'open' : ''}`}>
            <div className="px-3 py-2 border-b border-trade-600/20 mb-1">
              <div className="text-sm font-semibold text-trade-100">Trader</div>
              <div className="text-xs text-trade-400">trader@educatedtrades.io</div>
            </div>
            <div className="dropdown-item">
              <User className="w-4 h-4" />
              <span>My Profile</span>
            </div>
            <div className="dropdown-item">
              <Settings2 className="w-4 h-4" />
              <span>Account Settings</span>
            </div>
            <div className="dropdown-item">
              <HelpCircle className="w-4 h-4" />
              <span>Help & Support</span>
            </div>
            <div className="dropdown-divider" />
            <div className="dropdown-item text-red-400 hover:text-red-bright">
              <LogOut className="w-4 h-4" />
              <span>Disconnect</span>
            </div>
          </div>
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 overflow-y-auto relative z-10">
        {/* Header */}
        <header className="h-16 bg-trade-800/50 backdrop-blur-xl border-b border-trade-600/20 flex items-center justify-between px-4 lg:px-8 sticky top-0 z-30">
          <div className="flex items-center gap-4">
            <button 
              onClick={() => setSidebarOpen(true)}
              className="lg:hidden p-2 text-trade-300 hover:text-trade-100 hover:bg-trade-700/50 rounded-lg transition-colors"
            >
              <Menu className="w-5 h-5" />
            </button>
            
            {/* Search Bar */}
            <div className="hidden md:flex items-center gap-2">
              <div className="search-bar">
                <Search className="w-3.5 h-3.5 text-trade-400 shrink-0" />
                <input 
                  ref={searchInputRef}
                  type="text"
                  placeholder='Search symbols, patterns...'
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className=""
                />
                <kbd className="hidden lg:inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[9px] font-mono bg-trade-600/50 text-trade-500 border border-trade-600/30 shrink-0">
                  <span>⌘</span>K
                </kbd>
              </div>
            </div>

            <div className="hidden lg:flex items-center gap-2 text-sm">
              <span className="text-trade-400">System Status:</span>
              <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[10px] font-bold uppercase tracking-wider border ${
                killSwitchActive
                  ? 'bg-red-bright/10 text-red-bright border-red-bright/20'
                  : data.dailyLossHit
                    ? 'bg-red-bright/10 text-red-bright border-red-bright/20'
                    : 'bg-green-bright/10 text-green-bright border-green-bright/20'
              }`}>
                <span className={`w-1.5 h-1.5 rounded-full ${isHealthy ? 'bg-green-bright animate-pulse' : 'bg-red-bright'}`} />
                {killSwitchActive ? 'KILLED' : data.dailyLossHit ? 'LOSS LIMIT' : 'Healthy'}
              </div>
            </div>

            <div className="hidden md:flex items-center gap-2 text-sm">
              <span className="text-trade-400">Mode:</span>
              <span className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[10px] font-bold uppercase tracking-wider border ${
                data.systemHealth.mode === 'autonomous'
                  ? 'bg-accent-500/10 text-accent-400 border-accent-500/20'
                  : 'bg-amber-bright/10 text-amber-bright border-amber-bright/20'
              }`}>
                <Bot className="w-3 h-3" />
                {data.systemHealth.mode}
              </span>
            </div>

            <div className="hidden md:flex items-center gap-2 text-sm">
              <span className="text-trade-400">Cycle:</span>
              <span className="text-trade-200 font-mono text-xs font-semibold bg-trade-700/50 px-2 py-1 rounded-lg">
                #{data.systemHealth.cycleCount}
              </span>
            </div>
          </div>

          <div className="flex items-center gap-3">
            {/* Notification Bell */}
            <button 
              onClick={() => setIsNotificationsOpen(!isNotificationsOpen)}
              className="relative p-2.5 text-trade-300 hover:text-trade-100 hover:bg-trade-700/50 rounded-lg transition-all duration-200"
            >
              <Bell className={`w-5 h-5 ${bellShake ? 'animate-shake' : ''}`} />
              {hasUnreadNotifications && (
                <span className="absolute top-1.5 right-1.5 w-2.5 h-2.5 bg-red-bright rounded-full border-2 border-trade-800 animate-pulse" />
              )}
            </button>

            <div className="h-6 w-px bg-trade-600/30" />

            {/* Deploy / Stop Sniper Button */}
            <button 
              onClick={handleDeploySniper}
              disabled={isDeploying || killSwitchActive}
              className={`${
                data.systemHealth.mode === 'autonomous' 
                  ? 'btn-danger' 
                  : 'btn-primary'
              } text-xs px-4 py-2.5`}
            >
              <Zap className={`w-4 h-4 ${isDeploying ? 'animate-pulse' : ''}`} />
              {isDeploying ? 'Processing...' : data.systemHealth.mode === 'autonomous' ? 'Stop Sniper' : 'Deploy Sniper'}
            </button>
          </div>
        </header>

        <div className="p-4 lg:p-8 max-w-7xl mx-auto space-y-6 lg:space-y-8 animate-fade-in">
          {/* Quick Stats Summary Row */}
          <div className="animate-slide-up">
            <QuickStats
              totalTrades={totalTrades}
              winRate={data.portfolio.winRate}
              totalPnl={totalPnl}
              avgProfit={avgProfit}
              tradesToday={tradesToday}
              activeSignals={data.signals.length}
              patternCount={patternCount}
            />
          </div>

          {/* Stats Summary Row */}
          <div className="animate-slide-up-delay-1">
            {isLoading ? <SkeletonCard rows={2} /> : (
              <PortfolioCard portfolio={data.portfolio} mode={data.systemHealth.mode} isHealthy={isHealthy} />
            )}
          </div>

          {/* Risk & Safety Section */}
          <div className="animate-slide-up-delay-1">
            <div className="flex items-center gap-2.5 mb-5">
              <div className="w-7 h-7 rounded-lg bg-accent-500/10 flex items-center justify-center">
                <Shield className="w-4 h-4 text-accent-400" />
              </div>
              <h2 className="text-xs font-bold uppercase tracking-widest text-trade-400">Risk & Safety Controls</h2>
              <div className="flex-1 h-px bg-trade-600/20" />
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
              {isLoading ? (
                <>
                  <SkeletonCard /><SkeletonCard /><SkeletonCard />
                </>
              ) : (
                <>
                  <KillSwitchPanel 
                    killSwitchActive={killSwitchActive} 
                    onStatusChange={(active) => {
                      setKillSwitchActive(active);
                      if (active) {
                        setNotifications(prev => [{
                          id: `kill-${Date.now()}`,
                          type: 'alert',
                          title: '🔴 KILL SWITCH ACTIVATED',
                          description: 'Emergency kill switch has been triggered. All trading halted.',
                          timestamp: new Date().toLocaleTimeString(),
                          conviction: 0,
                          severity: 'critical'
                        }, ...prev]);
                        setIsNotificationsOpen(true);
                        triggerBellShake();
                      }
                    }}
                  />
                  <DailyLossTracker
                    dailyPnlPct={data.dailyPnlPct}
                    dailyLossLimit={data.dailyLossLimit}
                    dailyLossHit={data.dailyLossHit}
                  />
                  <HeartbeatStatus />
                </>
              )}
            </div>
          </div>

          {/* Drawdown & Overnight Risk Row */}
          <div className="animate-slide-up-delay-2 grid grid-cols-1 md:grid-cols-2 gap-4">
            {isLoading ? (
              <>
                <SkeletonCard /><SkeletonCard />
              </>
            ) : (
              <>
                <DrawdownDisplay equity={data.equity} peakEquity={peakEquity} />
                <OvernightRisk />
              </>
            )}
          </div>

          {/* Performance & Milestones Grid */}
          <div className="animate-slide-up-delay-2">
            <div className="flex items-center gap-2.5 mb-5">
              <div className="w-7 h-7 rounded-lg bg-accent-500/10 flex items-center justify-center">
                <TrendingUp className="w-4 h-4 text-accent-400" />
              </div>
              <h2 className="text-xs font-bold uppercase tracking-widest text-trade-400">Performance & Progress</h2>
              <div className="flex-1 h-px bg-trade-600/20" />
            </div>
            <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
              <div className="xl:col-span-2">
                {isLoading ? <SkeletonChart /> : <PerformanceChart data={data.chartData} />}
              </div>
              <div className="space-y-6">
                {isLoading ? (
                  <>
                    <SkeletonCard /><SkeletonCard />
                  </>
                ) : (
                  <>
                    <MilestoneTracker milestones={data.milestones} />
                    <TopPatterns patterns={data.topPatterns} />
                  </>
                )}
              </div>
            </div>
          </div>

          {/* Signals and History Grid */}
          <div className="animate-slide-up-delay-3">
            <div className="flex items-center gap-2.5 mb-5">
              <div className="w-7 h-7 rounded-lg bg-accent-500/10 flex items-center justify-center">
                <Activity className="w-4 h-4 text-accent-400" />
              </div>
              <h2 className="text-xs font-bold uppercase tracking-widest text-trade-400">Trading Activity</h2>
              <div className="flex-1 h-px bg-trade-600/20" />
            </div>
            <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
              <div className="xl:col-span-2">
                {isLoading ? <SkeletonTable /> : <HistoryTable trades={data.trades} />}
              </div>
              <div className="space-y-4">
                <h3 className="text-sm font-semibold text-trade-300 flex items-center gap-2 px-1">
                  <Signal className="w-4 h-4 text-accent-400" />
                  Live Signals
                </h3>
                {isLoading ? (
                  <SkeletonCard rows={4} />
                ) : (
                  data.signals.length > 0 ? (
                    data.signals.map(signal => (
                      <SignalCard key={signal.id} signal={signal} />
                    ))
                  ) : (
                    <div className="card p-8 text-center">
                      <div className="w-12 h-12 rounded-xl bg-trade-700/50 flex items-center justify-center mx-auto mb-4">
                        <Signal className="w-6 h-6 text-trade-400" />
                      </div>
                      <p className="text-trade-400 text-sm">No active signals</p>
                      <p className="text-trade-500 text-xs mt-1">Waiting for market data...</p>
                    </div>
                  )
                )}
              </div>
            </div>
          </div>
        </div>
      </main>

      {/* Notifications Sidebar */}
      <div className={`fixed inset-y-0 right-0 w-80 bg-trade-800/98 backdrop-blur-xl shadow-2xl border-l border-trade-600/20 transform transition-transform duration-300 ease-in-out z-50 ${isNotificationsOpen ? 'translate-x-0' : 'translate-x-full'}`}>
        <div className="p-6 h-full flex flex-col">
          <div className="flex items-center justify-between mb-6">
            <h2 className="text-base font-bold flex items-center gap-2.5">
              <div className="w-7 h-7 rounded-lg bg-accent-500/10 flex items-center justify-center">
                <Bell className="w-4 h-4 text-accent-400" />
              </div>
              Notifications
              {notificationCount > 0 && (
                <span className="badge-red text-[9px]">{notificationCount}</span>
              )}
            </h2>
            <button 
              onClick={() => setIsNotificationsOpen(false)} 
              className="p-2 text-trade-300 hover:text-trade-100 hover:bg-trade-700/50 rounded-lg transition-colors"
            >
              <X className="w-4 h-4" />
            </button>
          </div>

          <div className="flex-1 overflow-y-auto space-y-3 pr-2 custom-scrollbar">
            {notifications.length === 0 ? (
              <div className="flex flex-col items-center justify-center h-64 text-trade-400 space-y-3">
                <div className="w-16 h-16 rounded-2xl bg-trade-700/30 flex items-center justify-center">
                  <Bell className="w-8 h-8 opacity-30" />
                </div>
                <p className="text-sm font-medium">No recent activity</p>
                <p className="text-xs text-trade-500">New notifications will appear here</p>
              </div>
            ) : (
              notifications.map((notif, i) => (
                <div key={notif.id || i} className={`p-4 rounded-xl border space-y-2 transition-all duration-200 ${
                  notif.type === 'alert' 
                    ? notif.severity === 'critical' || notif.severity === 'error'
                      ? 'bg-red-bright/5 border-red-bright/20'
                      : 'bg-amber-bright/5 border-amber-bright/20'
                    : 'bg-trade-700/30 border-trade-600/20'
                }`}>
                  <div className="flex justify-between items-start gap-2">
                    <div className="flex items-start gap-2.5">
                      {notif.type === 'alert' ? (
                        <AlertCircle className={`w-4 h-4 mt-0.5 shrink-0 ${
                          notif.severity === 'critical' || notif.severity === 'error'
                            ? 'text-red-bright'
                            : 'text-amber-bright'
                        }`} />
                      ) : (
                        <Zap className="w-4 h-4 mt-0.5 text-accent-400 shrink-0" />
                      )}
                      <div>
                        <h3 className={`font-semibold text-sm ${
                          notif.type === 'alert'
                            ? notif.severity === 'critical' || notif.severity === 'error'
                              ? 'text-red-bright'
                              : 'text-amber-bright'
                            : 'text-trade-100'
                        }`}>{notif.title}</h3>
                        <p className="text-xs text-trade-400 mt-1">{notif.description}</p>
                      </div>
                    </div>
                    <span className="text-[9px] text-trade-500 font-mono whitespace-nowrap">{notif.timestamp}</span>
                  </div>
                  
                  {notif.type === 'trade' && (
                    <div className="pt-2 flex items-center gap-2">
                      <span className="text-[9px] uppercase font-bold text-trade-500">Conviction:</span>
                      <div className="flex-1 h-1 bg-trade-600/50 rounded-full overflow-hidden">
                        <div 
                          className={`h-full rounded-full transition-all duration-500 ${notif.conviction > 0 ? 'bg-green-bright' : 'bg-red-bright'}`}
                          style={{ width: `${Math.abs(notif.conviction) * 100}%` }}
                        />
                      </div>
                    </div>
                  )}

                  {notif.type === 'alert' && (
                    <div className="flex items-center gap-1.5 pt-1">
                      <span className={`text-[9px] uppercase font-bold tracking-wider ${
                        notif.severity === 'critical' || notif.severity === 'error'
                          ? 'text-red-bright'
                          : 'text-amber-bright'
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
              className="mt-5 w-full py-3 text-sm text-trade-400 hover:text-accent-400 font-semibold transition-colors rounded-lg hover:bg-trade-700/30"
            >
              Clear all notifications
            </button>
          )}
        </div>
      </div>
      </div>

      {/* Bottom Status Bar */}
      <StatusBar 
        lastUpdate={lastUpdateTime}
        apiConnected={data.systemHealth.running}
        cycleCount={data.systemHealth.cycleCount}
      />
    </div>
  );
}