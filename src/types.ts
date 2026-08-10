export type Trade = {
  id: string;
  symbol: string;
  side: 'buy' | 'sell';
  entryPrice: number;
  exitPrice?: number;
  amount: number;
  status: 'open' | 'closed';
  pnl?: number;
  timestamp: string;
};

export type Signal = {
  id: string;
  symbol: string;
  sentiment: number; // -1 to 1
  patternConviction: number; // 0 to 1
  recommendation: 'strong_buy' | 'buy' | 'neutral' | 'sell' | 'strong_sell';
  timestamp: string;
};

export type Portfolio = {
  balance: number;
  equity: number;
  pnlDay: number;
  pnlDayPercent: number;
  winRate: number;
};

export type Alert = {
  id: number;
  type: string;
  message: string;
  severity: 'info' | 'warning' | 'error' | 'critical';
  timestamp: string;
};

export type Milestone = {
  target: number;
  current: number;
  remaining: number;
  percent: number;
  is_reached: boolean;
};

export type PatternSummary = {
  signature: string;
  count: number;
  win_rate: number;
  avg_profit_pct: number;
  signal_strength: number;
  is_robust: boolean;
};
