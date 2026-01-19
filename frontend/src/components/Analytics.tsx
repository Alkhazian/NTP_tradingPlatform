import { useState, useEffect, useRef, useMemo } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from './ui/card';
import { createChart, ColorType } from 'lightweight-charts';
import {
    Activity,
    TrendingUp,
    TrendingDown,
    DollarSign,
    Percent,
    ArrowUpRight,
    ArrowDownRight,
    Filter
} from 'lucide-react';

interface Strategy {
    id: string;
    is_running: boolean;
}

interface Trade {
    id: number;
    strategy_id: string;
    instrument_id: string;
    entry_time: string;
    entry_price: number;
    exit_time: string | null;
    exit_price: number | null;
    exit_reason: string | null;
    trade_type: string;
    quantity: number;
    direction: string;
    pnl: number | null;
    commission: number;
    result: 'WIN' | 'LOSS' | 'BREAKEVEN' | null;
}

interface Stats {
    total_trades: number;
    win_rate: number;
    total_pnl: number;
    total_commission: number;
    net_pnl: number;
    max_win: number;
    max_loss: number;
}

const getUrl = (path: string) => {
    const base = (import.meta.env.VITE_API_URL || '').replace(/\/$/, '');
    return `${base}${path.startsWith('/') ? path : '/' + path}`;
};

export default function Analytics() {
    const [strategies, setStrategies] = useState<Strategy[]>([]);
    const [selectedStrategy, setSelectedStrategy] = useState<string>('');
    const [stats, setStats] = useState<Stats | null>(null);
    const [trades, setTrades] = useState<Trade[]>([]);
    const [isLoading, setIsLoading] = useState(false);

    // Filters
    const [showFilters, setShowFilters] = useState(false);
    const [filterResult, setFilterResult] = useState<string>('ALL'); // ALL, WIN, LOSS

    // Chart Ref
    const chartContainerRef = useRef<HTMLDivElement>(null);

    // Fetch Strategies on Mount
    useEffect(() => {
        fetch(getUrl('/strategies'))
            .then(res => res.json())
            .then(data => {
                const strats = Array.isArray(data) ? data : [];
                setStrategies(strats);
                if (strats.length > 0) {
                    setSelectedStrategy(strats[0].id);
                }
            })
            .catch(console.error);
    }, []);

    // Fetch Data when Strategy Changes
    useEffect(() => {
        if (!selectedStrategy) return;

        setIsLoading(true);
        Promise.all([
            fetch(getUrl(`/strategies/${selectedStrategy}/stats`)).then(res => res.json()),
            fetch(getUrl(`/strategies/${selectedStrategy}/trades?limit=0`)).then(res => res.json())
        ]).then(([statsData, tradesData]) => {
            setStats(statsData);
            setTrades(tradesData);
        }).catch(console.error)
            .finally(() => setIsLoading(false));
    }, [selectedStrategy]);

    // Calculate Equity Curve Data
    const chartData = useMemo(() => {
        const sortedTrades = [...trades].sort((a, b) => new Date(a.entry_time).getTime() - new Date(b.entry_time).getTime());

        let runningPnL = 0;
        const data = sortedTrades
            .filter(t => t.exit_time && t.pnl !== null)
            .map(t => {
                const net = (t.pnl || 0) - (t.commission || 0);
                runningPnL += net;
                return {
                    time: new Date(t.exit_time || t.entry_time).getTime() / 1000 as any,
                    value: runningPnL
                };
            });

        const uniqueData: { time: any, value: number }[] = [];
        data.forEach(point => {
            if (uniqueData.length > 0 && uniqueData[uniqueData.length - 1].time >= point.time) {
                point.time = uniqueData[uniqueData.length - 1].time + 1;
            }
            uniqueData.push(point);
        });
        return uniqueData;
    }, [trades]);

    // Render Chart
    useEffect(() => {
        if (!chartContainerRef.current || chartData.length === 0) return;

        const chart = createChart(chartContainerRef.current, {
            layout: {
                background: { type: ColorType.Solid, color: 'transparent' },
                textColor: '#d1d5db',
            },
            grid: {
                vertLines: { color: 'rgba(255, 255, 255, 0.1)' },
                horzLines: { color: 'rgba(255, 255, 255, 0.1)' },
            },
            width: chartContainerRef.current.clientWidth,
            height: 300,
            timeScale: {
                timeVisible: true,
                secondsVisible: false,
            }
        });

        const areaSeries = chart.addBaselineSeries({
            baseValue: { type: 'price', price: 0 },
            topLineColor: '#2bd698',
            topFillColor1: 'rgba(43, 214, 152, 0.4)',
            topFillColor2: 'rgba(43, 214, 152, 0.0)',
            bottomLineColor: '#ef4444',
            bottomFillColor1: 'rgba(239, 68, 68, 0.0)',
            bottomFillColor2: 'rgba(239, 68, 68, 0.4)',
            lineWidth: 2,
        });

        areaSeries.setData(chartData);
        chart.timeScale().fitContent();

        const handleResize = () => {
            if (chartContainerRef.current) {
                chart.applyOptions({ width: chartContainerRef.current.clientWidth });
            }
        };

        window.addEventListener('resize', handleResize);

        return () => {
            window.removeEventListener('resize', handleResize);
            chart.remove();
        };
    }, [chartData]); // Depend on processed data, not raw trades to avoid recalc loop if memozied correctly


    // Sorting
    const [sortConfig, setSortConfig] = useState<{ key: keyof Trade | 'net_pnl', direction: 'asc' | 'desc' }>({ key: 'entry_time', direction: 'desc' });

    const handleSort = (key: keyof Trade | 'net_pnl') => {
        let direction: 'asc' | 'desc' = 'asc';
        if (sortConfig.key === key && sortConfig.direction === 'asc') {
            direction = 'desc';
        }
        setSortConfig({ key, direction });
    };

    // Filter AND Sort Trades for Table
    const sortedTrades = useMemo(() => {
        let items = [...trades];

        // Filtering
        if (filterResult !== 'ALL') {
            items = items.filter(t => t.result === filterResult);
        }

        // Sorting
        items.sort((a, b) => {
            let aValue: any = a[sortConfig.key as keyof Trade];
            let bValue: any = b[sortConfig.key as keyof Trade];

            // Handle computed Net PnL
            if (sortConfig.key === 'net_pnl') {
                aValue = (a.pnl || 0) - (a.commission || 0);
                bValue = (b.pnl || 0) - (b.commission || 0);
            }

            // Handle dates
            if (sortConfig.key === 'entry_time') {
                return sortConfig.direction === 'asc'
                    ? new Date(a.entry_time).getTime() - new Date(b.entry_time).getTime()
                    : new Date(b.entry_time).getTime() - new Date(a.entry_time).getTime();
            }

            if (aValue < bValue) return sortConfig.direction === 'asc' ? -1 : 1;
            if (aValue > bValue) return sortConfig.direction === 'asc' ? 1 : -1;
            return 0;
        });

        return items;
    }, [trades, filterResult, sortConfig]);

    const formatCurrency = (val: number) => {
        return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(val);
    };

    return (
        <div className="space-y-6 animate-in fade-in duration-500 pb-10">
            {/* Header / Strategy Selector */}
            <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
                <div className="flex items-center gap-2 bg-black/40 p-1.5 rounded-lg border border-white/10">
                    <span className="text-sm font-medium px-2 text-white/60">Strategy:</span>
                    <select
                        value={selectedStrategy}
                        onChange={(e) => setSelectedStrategy(e.target.value)}
                        className="bg-transparent text-white font-mono text-sm focus:outline-none min-w-[200px] [&>option]:bg-zinc-900"
                        disabled={isLoading}
                    >
                        {strategies.map(s => (
                            <option key={s.id} value={s.id}>{s.id}</option>
                        ))}
                    </select>
                    {isLoading && <Activity className="w-4 h-4 animate-spin text-white/40" />}
                </div>
            </div>

            {/* Stats Cards */}
            {stats && (
                <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
                    <StatsCard
                        title="Net PnL"
                        value={formatCurrency(stats.net_pnl)}
                        icon={<DollarSign className="w-4 h-4" />}
                        trend={stats.net_pnl >= 0 ? 'up' : 'down'}
                    />
                    <StatsCard
                        title="Total Trades"
                        value={stats.total_trades.toString()}
                        icon={<Activity className="w-4 h-4 text-blue-400" />}
                    />
                    <StatsCard
                        title="Win Rate"
                        value={`${stats.win_rate}%`}
                        icon={<Percent className="w-4 h-4" />}
                    />
                    <StatsCard
                        title="Max Win"
                        value={formatCurrency(stats.max_win)}
                        icon={<TrendingUp className="w-4 h-4 text-emerald-400" />}
                        trend="up"
                        className="border-emerald-500/20"
                    />
                    <StatsCard
                        title="Max Loss"
                        value={formatCurrency(stats.max_loss)}
                        icon={<TrendingDown className="w-4 h-4 text-red-400" />}
                        trend="down"
                        className="border-red-500/20"
                    />
                    <StatsCard
                        title="Total Comm."
                        value={formatCurrency(stats.total_commission)}
                        icon={<Activity className="w-4 h-4 text-orange-400" />}
                    />
                </div>
            )}

            {/* Equity Graph */}
            <Card variant="glass" className="p-1">
                <CardHeader className="pb-2">
                    <CardTitle className="text-lg flex items-center gap-2">
                        <TrendingUp className="w-5 h-5 text-emerald-400" />
                        Equity Curve
                    </CardTitle>
                </CardHeader>
                <CardContent>
                    <div ref={chartContainerRef} className="w-full h-[300px]" />
                </CardContent>
            </Card>

            {/* Trade List */}
            <Card variant="glass">
                <CardHeader className="flex flex-row items-center justify-between">
                    <CardTitle className="text-lg">Trade History</CardTitle>
                    <div className="flex items-center gap-2">
                        <button
                            onClick={() => setShowFilters(!showFilters)}
                            className={`p-2 rounded-md transition-colors ${showFilters ? 'bg-white/10 text-white' : 'text-muted-foreground hover:bg-white/5'}`}
                        >
                            <Filter className="w-4 h-4" />
                        </button>
                    </div>
                </CardHeader>

                {showFilters && (
                    <div className="px-6 pb-4 flex gap-4 border-b border-white/5">
                        <div className="flex flex-col gap-1">
                            <label className="text-xs text-muted-foreground">Result</label>
                            <select
                                value={filterResult}
                                onChange={(e) => setFilterResult(e.target.value)}
                                className="bg-black/20 border border-white/10 rounded px-2 py-1 text-sm text-white"
                            >
                                <option value="ALL">All Outcomes</option>
                                <option value="WIN">Win</option>
                                <option value="LOSS">Loss</option>
                            </select>
                        </div>
                    </div>
                )}

                <CardContent className="p-0">
                    <div className="overflow-x-auto">
                        <table className="w-full text-sm text-left">
                            <thead className="bg-white/5 text-muted-foreground font-medium uppercase text-xs">
                                <tr>
                                    <th className="px-4 py-3 cursor-pointer hover:text-white transition-colors" onClick={() => handleSort('entry_time')}>
                                        <div className="flex items-center gap-1">Time {sortConfig.key === 'entry_time' && (sortConfig.direction === 'asc' ? '↑' : '↓')}</div>
                                    </th>
                                    <th className="px-4 py-3 cursor-pointer hover:text-white transition-colors" onClick={() => handleSort('instrument_id')}>
                                        <div className="flex items-center gap-1">Symbol {sortConfig.key === 'instrument_id' && (sortConfig.direction === 'asc' ? '↑' : '↓')}</div>
                                    </th>
                                    <th className="px-4 py-3 cursor-pointer hover:text-white transition-colors" onClick={() => handleSort('direction')}>
                                        <div className="flex items-center gap-1">Side {sortConfig.key === 'direction' && (sortConfig.direction === 'asc' ? '↑' : '↓')}</div>
                                    </th>
                                    <th className="px-4 py-3 text-right cursor-pointer hover:text-white transition-colors" onClick={() => handleSort('entry_price')}>
                                        <div className="flex items-center justify-end gap-1">Entry {sortConfig.key === 'entry_price' && (sortConfig.direction === 'asc' ? '↑' : '↓')}</div>
                                    </th>
                                    <th className="px-4 py-3 text-right cursor-pointer hover:text-white transition-colors" onClick={() => handleSort('exit_price')}>
                                        <div className="flex items-center justify-end gap-1">Exit {sortConfig.key === 'exit_price' && (sortConfig.direction === 'asc' ? '↑' : '↓')}</div>
                                    </th>
                                    <th className="px-4 py-3 text-right cursor-pointer hover:text-white transition-colors" onClick={() => handleSort('net_pnl')}>
                                        <div className="flex items-center justify-end gap-1">PnL (Net) {sortConfig.key === 'net_pnl' && (sortConfig.direction === 'asc' ? '↑' : '↓')}</div>
                                    </th>
                                    <th className="px-4 py-3 cursor-pointer hover:text-white transition-colors" onClick={() => handleSort('exit_reason')}>
                                        <div className="flex items-center gap-1">Exit Reason {sortConfig.key === 'exit_reason' && (sortConfig.direction === 'asc' ? '↑' : '↓')}</div>
                                    </th>
                                    <th className="px-4 py-3 text-center cursor-pointer hover:text-white transition-colors" onClick={() => handleSort('result')}>
                                        <div className="flex items-center justify-center gap-1">Result {sortConfig.key === 'result' && (sortConfig.direction === 'asc' ? '↑' : '↓')}</div>
                                    </th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-white/5">
                                {sortedTrades.map((trade) => {
                                    const netPnl = (trade.pnl || 0) - (trade.commission || 0);
                                    const isWin = netPnl > 0;

                                    return (
                                        <tr key={trade.id} className="hover:bg-white/5 transition-colors">
                                            <td className="px-4 py-3 font-mono text-xs text-white/60">
                                                {new Date(trade.entry_time).toLocaleString()}
                                            </td>
                                            <td className="px-4 py-3 font-medium text-white/90">
                                                {trade.instrument_id.split('=')[0]}
                                                <span className="text-xs text-muted-foreground ml-1 opacity-50">
                                                    {trade.instrument_id.split('.')[1]}
                                                </span>
                                            </td>
                                            <td className="px-4 py-3">
                                                <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${trade.direction === 'BUY'
                                                    ? 'bg-orange-500/10 text-orange-400'
                                                    : 'bg-blue-500/10 text-blue-400'
                                                    }`}>
                                                    {trade.direction}
                                                </span>
                                            </td>
                                            <td className="px-4 py-3 text-right font-mono">
                                                {trade.entry_price.toFixed(2)}
                                            </td>
                                            <td className="px-4 py-3 text-right font-mono text-white/60">
                                                {trade.exit_price !== null ? trade.exit_price.toFixed(2) : '-'}
                                            </td>
                                            <td className={`px-4 py-3 text-right font-mono font-medium ${isWin ? 'text-emerald-400' : (netPnl < 0 ? 'text-red-400' : 'text-gray-400')
                                                }`}>
                                                {netPnl !== 0 ? (isWin ? '+' : '') + formatCurrency(netPnl) : '-'}
                                            </td>
                                            <td className="px-4 py-3">
                                                {trade.exit_reason ? (
                                                    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-white/5 text-white/80">
                                                        {trade.exit_reason}
                                                    </span>
                                                ) : (
                                                    <span className="text-white/20">-</span>
                                                )}
                                            </td>
                                            <td className="px-4 py-3 text-center">
                                                {trade.result ? (
                                                    <span className={`inline-flex items-center gap-1 text-xs ${trade.result === 'WIN' ? 'text-emerald-400' :
                                                        trade.result === 'LOSS' ? 'text-red-400' : 'text-yellow-400'
                                                        }`}>
                                                        {trade.result === 'WIN' ? <ArrowUpRight className="w-3 h-3" /> :
                                                            trade.result === 'LOSS' ? <ArrowDownRight className="w-3 h-3" /> : null}
                                                        {trade.result}
                                                    </span>
                                                ) : (
                                                    <span className="text-white/20">-</span>
                                                )}
                                            </td>
                                        </tr>
                                    );
                                })}
                                {sortedTrades.length === 0 && (
                                    <tr>
                                        <td colSpan={8} className="px-4 py-8 text-center text-muted-foreground italic">
                                            No trades found for this strategy.
                                        </td>
                                    </tr>
                                )}
                            </tbody>
                        </table>
                    </div>
                </CardContent>
            </Card>
        </div>
    );
}

function StatsCard({ title, value, icon, subtext, trend, className = '' }: any) {
    return (
        <Card variant="glass" className={`p-4 flex flex-col justify-between ${className}`}>
            <div className="flex justify-between items-start mb-2">
                <span className="text-sm text-muted-foreground font-medium">{title}</span>
                <div className={`p-1.5 rounded-lg bg-white/5 text-white/60 ${trend === 'up' ? 'text-emerald-400 bg-emerald-500/10' : trend === 'down' ? 'text-red-400 bg-red-500/10' : ''}`}>
                    {icon}
                </div>
            </div>
            <div>
                <div className={`text-2xl font-bold tracking-tight font-mono ${trend === 'up' ? 'text-emerald-400' : trend === 'down' ? 'text-red-400' : 'text-white'}`}>
                    {value}
                </div>
                {subtext && <p className="text-xs text-muted-foreground mt-1">{subtext}</p>}
            </div>
        </Card>
    );
}
