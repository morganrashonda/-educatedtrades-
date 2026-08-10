import type { Portfolio, Signal, Trade } from "./types";

export const mockPortfolio: Portfolio = {
  balance: 9540.25,
  equity: 10842.15,
  pnlDay: 432.50,
  pnlDayPercent: 4.15,
  winRate: 88.5
};

export const mockSignals: Signal[] = [
  {
    id: '1',
    symbol: 'NVDA',
    sentiment: 0.85,
    patternConviction: 0.92,
    recommendation: 'strong_buy',
    timestamp: new Date().toISOString()
  },
  {
    id: '2',
    symbol: 'TSLA',
    sentiment: -0.45,
    patternConviction: 0.65,
    recommendation: 'sell',
    timestamp: new Date().toISOString()
  },
  {
    id: '3',
    symbol: 'AAPL',
    sentiment: 0.15,
    patternConviction: 0.32,
    recommendation: 'neutral',
    timestamp: new Date().toISOString()
  }
];

export const mockTrades: Trade[] = [
  {
    id: '1',
    symbol: 'NVDA',
    side: 'buy',
    entryPrice: 125.40,
    exitPrice: 128.50,
    amount: 10,
    status: 'closed',
    pnl: 31.00,
    timestamp: new Date(Date.now() - 1000 * 60 * 15).toISOString()
  },
  {
    id: '2',
    symbol: 'AMD',
    side: 'sell',
    entryPrice: 160.20,
    exitPrice: 158.10,
    amount: 5,
    status: 'closed',
    pnl: 10.50,
    timestamp: new Date(Date.now() - 1000 * 60 * 45).toISOString()
  },
  {
    id: '3',
    symbol: 'BTC',
    side: 'buy',
    entryPrice: 64200.00,
    amount: 0.1,
    status: 'open',
    timestamp: new Date(Date.now() - 1000 * 60 * 120).toISOString()
  }
];
