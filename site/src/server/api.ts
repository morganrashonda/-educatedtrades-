import type { Portfolio, Signal, Trade, Alert, Milestone, PatternSummary } from '../types';

const API_BASE = 'http://localhost:3099/api';

export async function getSystemStatus() {
  try {
    const response = await fetch(`${API_BASE}/status`);
    if (!response.ok) throw new Error('API request failed');
    return await response.json();
  } catch (error) {
    console.error('Error fetching system status:', error);
    return null;
  }
}

export async function getPortfolio() {
  try {
    const response = await fetch(`${API_BASE}/portfolio`);
    if (!response.ok) throw new Error('API request failed');
    return await response.json();
  } catch (error) {
    console.error('Error fetching portfolio:', error);
    return null;
  }
}

export async function getLatestSentiment() {
  try {
    const response = await fetch(`${API_BASE}/sentiment/latest`);
    if (!response.ok) throw new Error('API request failed');
    return await response.json();
  } catch (error) {
    console.error('Error fetching sentiment:', error);
    return null;
  }
}

export async function getPatternSummary() {
  try {
    const response = await fetch(`${API_BASE}/patterns/top`);
    if (!response.ok) throw new Error('API request failed');
    return await response.json();
  } catch (error) {
    console.error('Error fetching pattern summary:', error);
    return null;
  }
}

export async function getRecentTrades() {
  try {
    const response = await fetch(`${API_BASE}/trades/recent`);
    if (!response.ok) throw new Error('API request failed');
    const data = await response.json();
    return data.trades || [];
  } catch (error) {
    console.error('Error fetching recent trades:', error);
    return [];
  }
}

export async function getRecentAlerts(): Promise<Alert[]> {
  try {
    const response = await fetch(`${API_BASE}/alerts/recent`);
    if (!response.ok) throw new Error('API request failed');
    const data = await response.json();
    return data.alerts || [];
  } catch (error) {
    console.error('Error fetching recent alerts:', error);
    return [];
  }
}

export async function getMilestoneData() {
  try {
    const response = await fetch(`${API_BASE}/milestones`);
    if (!response.ok) throw new Error('API request failed');
    return await response.json();
  } catch (error) {
    console.error('Error fetching milestones:', error);
    return null;
  }
}

export async function setOrchestratorMode(mode: 'manual' | 'autonomous' | 'stopped') {
  try {
    const response = await fetch(`${API_BASE}/mode`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode })
    });
    if (!response.ok) throw new Error('API request failed');
    return await response.json();
  } catch (error) {
    console.error('Error setting mode:', error);
    return null;
  }
}

export async function getDashboardData() {
  try {
    const [status, portfolioRaw, sentiment, patterns, tradesRaw, alerts, milestoneRaw] = await Promise.all([
      getSystemStatus(),
      getPortfolio(),
      getLatestSentiment(),
      getPatternSummary(),
      getRecentTrades(),
      getRecentAlerts(),
      getMilestoneData()
    ]);

    const orchestrator = status?.orchestrator || {};

    const milestone: Milestone = {
      target: milestoneRaw?.profit_target || 10000,
      current: milestoneRaw?.cumulative_pnl || 0,
      remaining: milestoneRaw?.remaining || 10000,
      percent: milestoneRaw?.progress_pct || 0,
      is_reached: milestoneRaw?.progress_pct >= 100
    };

    const portfolio: Portfolio = {
      balance: portfolioRaw?.buying_power || 0,
      equity: portfolioRaw?.equity || 0,
      pnlDay: milestone.current, 
      pnlDayPercent: milestone.percent,
      winRate: patterns?.summary?.total_patterns > 0 
        ? Math.round((patterns.summary.robust_patterns / patterns.summary.total_patterns) * 100) 
        : 0
    };

    const trades: Trade[] = (tradesRaw || []).map((t: any) => ({
      id: t.order_id || Math.random().toString(),
      symbol: t.symbol,
      side: t.side || 'buy',
      entryPrice: t.filled_price || 0,
      amount: t.quantity || 0,
      status: t.status === 'filled' ? 'closed' : 'open',
      pnl: t.pnl || 0,
      timestamp: new Date(t.timestamp * 1000).toISOString()
    }));

    const signals: Signal[] = [];
    if (sentiment && sentiment.conviction_score !== undefined) {
      signals.push({
        id: 'sentiment-main',
        symbol: 'MARKET',
        sentiment: sentiment.conviction_score,
        patternConviction: patterns?.summary?.learned_weights?.sentiment_mult || 0.5,
        recommendation: sentiment.consensus === 'bullish' ? 'buy' : sentiment.consensus === 'bearish' ? 'sell' : 'neutral',
        timestamp: new Date().toISOString()
      });
    }

    const topPatterns: PatternSummary[] = (patterns?.summary?.top_patterns || []).map((p: any) => ({
      signature: p.pattern_id, // Use pattern_id from response
      count: p.count,
      win_rate: p.win_rate,
      avg_profit_pct: p.avg_profit_pct,
      signal_strength: p.signal_strength,
      is_robust: p.is_robust
    }));

    // Generate chart data from milestone history
    const history = (milestoneRaw?.history || []).reverse();
    const chartData = history.map((h: any) => ({
      time: new Date(h.timestamp * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      value: 100000 + h.cumulative_pnl, // Assuming 100k start
      sentiment: 0 // Sentiment not in milestone history
    }));

    // Fallback if no history
    if (chartData.length === 0) {
      chartData.push({ time: '09:00', value: 100000, sentiment: 0 });
      chartData.push({ time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }), value: portfolio.equity, sentiment: 0 });
    }

    return {
      portfolio,
      signals,
      trades,
      alerts,
      milestones: [milestone],
      topPatterns,
      chartData,
      systemHealth: {
        mode: orchestrator.mode || 'manual',
        running: orchestrator.running || false,
        cycleCount: orchestrator.cycle_count || 0,
        lastCycle: orchestrator.last_cycle_time ? new Date(orchestrator.last_cycle_time * 1000).toISOString() : null
      }
    };
  } catch (error) {
    console.error('Error fetching dashboard data:', error);
    return {
      portfolio: { balance: 0, equity: 0, pnlDay: 0, pnlDayPercent: 0, winRate: 0 },
      signals: [],
      trades: [],
      alerts: [],
      milestones: [],
      topPatterns: [],
      chartData: [],
      systemHealth: { mode: 'manual', running: false, cycleCount: 0, lastCycle: null }
    };
  }
}
