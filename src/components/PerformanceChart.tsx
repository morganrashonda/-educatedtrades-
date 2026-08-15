import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { TrendingUp } from 'lucide-react';

const defaultData = [
  { time: '09:00', value: 100000, sentiment: 0.2 },
  { time: '10:00', value: 100200, sentiment: 0.5 },
  { time: '11:00', value: 100150, sentiment: 0.3 },
  { time: '12:00', value: 100400, sentiment: 0.8 },
  { time: '13:00', value: 100300, sentiment: 0.1 },
  { time: '14:00', value: 100550, sentiment: 0.6 },
  { time: '15:00', value: 100800, sentiment: 0.9 },
];

export function PerformanceChart({ data = defaultData }: { data?: any[] }) {
  return (
    <div className="card p-6 h-[380px]">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-lg bg-accent-500/10 flex items-center justify-center">
            <TrendingUp className="w-4.5 h-4.5 text-accent-400" />
          </div>
          <div>
            <h3 className="font-bold text-sm text-trade-100">Equity Growth & Sentiment</h3>
            <span className="text-[10px] text-trade-400">Real-time performance tracking</span>
          </div>
        </div>
        <div className="flex gap-4">
          <div className="flex items-center gap-1.5">
            <div className="w-2.5 h-2.5 rounded-full bg-accent-500" />
            <span className="text-[10px] text-trade-400 font-medium">Equity</span>
          </div>
          <div className="flex items-center gap-1.5">
            <div className="w-2.5 h-2.5 rounded-full bg-green-bright" />
            <span className="text-[10px] text-trade-400 font-medium">Sentiment</span>
          </div>
        </div>
      </div>
      
      <div className="h-[260px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 5, right: 5, left: 5, bottom: 5 }}>
            <defs>
              <linearGradient id="colorValue" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#6366f1" stopOpacity={0.15}/>
                <stop offset="95%" stopColor="#6366f1" stopOpacity={0}/>
              </linearGradient>
              <linearGradient id="colorSentiment" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#22d65e" stopOpacity={0.12}/>
                <stop offset="95%" stopColor="#22d65e" stopOpacity={0}/>
              </linearGradient>
              <filter id="glow">
                <feGaussianBlur stdDeviation="2" result="coloredBlur"/>
                <feMerge>
                  <feMergeNode in="coloredBlur"/>
                  <feMergeNode in="SourceGraphic"/>
                </feMerge>
              </filter>
            </defs>
            <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#1a2332" />
            <XAxis 
              dataKey="time" 
              axisLine={false} 
              tickLine={false} 
              tick={{ fontSize: 10, fill: '#6b7d94' }}
              dy={10}
            />
            <YAxis 
              hide 
              domain={['dataMin - 500', 'dataMax + 500']}
            />
            <Tooltip 
              contentStyle={{ 
                borderRadius: '12px', 
                border: '1px solid rgba(99, 102, 241, 0.2)',
                background: 'rgba(17, 24, 39, 0.95)',
                boxShadow: '0 8px 32px rgba(0, 0, 0, 0.4)',
                fontSize: '12px',
                color: '#e2e8f0',
                backdropFilter: 'blur(12px)',
                padding: '12px 16px',
              }}
              cursor={{ stroke: '#6366f1', strokeWidth: 1, strokeDasharray: '4 4' }}
            />
            <Area 
              type="monotone" 
              dataKey="sentiment" 
              stroke="#22d65e" 
              strokeWidth={2}
              fillOpacity={1} 
              fill="url(#colorSentiment)" 
              dot={false}
            />
            <Area 
              type="monotone" 
              dataKey="value" 
              stroke="#6366f1" 
              strokeWidth={2.5}
              fillOpacity={1} 
              fill="url(#colorValue)" 
              dot={false}
              activeDot={{ r: 5, fill: '#6366f1', stroke: '#fff', strokeWidth: 2 }}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}