import { useEffect, useState, useCallback } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from './ui/card';
import { Badge } from './ui/badge';
import { StatCard } from './ui/stat-card';
import { SystemStatusPanel } from './ui/status-indicator';
import { Header, Sidebar, SidebarItem } from './layout';
import { Icons } from './ui/icons';
import Strategies from './Strategies';
import Analytics from './Analytics';
import LogViewer from './LogViewer';

interface SystemStatus {
    connected: boolean;
    nautilus_active: boolean;
    net_liquidation: string;
    buying_power: string;
    account_currency: string;
    account_id: string | null;
    redis_connected: boolean;
    backend_connected: boolean;
    open_positions: number;
    positions?: Position[];
    day_realized_pnl?: string;
    // Portfolio metrics
    margin_used?: string;
    margin_available?: string;
    margin_usage_percent?: string;
    total_unrealized_pnl?: string;
    total_realized_pnl?: string;
    net_exposure?: string;
    leverage?: string;
    recent_trades?: Trade[];
    strategies?: Array<{
        id: string;
        running: boolean;
        status: string;
        config?: {
            name?: string;
        };
        positions?: Array<{
            symbol: string;
            side: string;
            quantity: number;
            entry_price: number;
            current_price: number;
            unrealized_pnl: number;
        }>;
    }>;
}

interface Position {
    symbol: string;
    quantity: number;
    avg_price: number;
    unrealized_pnl: number;
}

interface Trade {
    type: 'buy' | 'sell';
    symbol: string;
    quantity: number;
    price: number;
    time: string;
    timestamp: number;
}


export default function Dashboard() {
    const [status, setStatus] = useState<SystemStatus>({
        connected: false,
        nautilus_active: false,
        net_liquidation: "N/A",
        buying_power: "N/A",
        account_currency: "USD",
        account_id: null,
        redis_connected: false,
        backend_connected: false,
        open_positions: 0,
        positions: [],
        day_realized_pnl: "0.0 USD",
        recent_trades: []
    });


    //const [activeNav, setActiveNav] = useState('logs');
    const [activeNav, setActiveNav] = useState('dashboard');
    const [isSidebarOpen, setIsSidebarOpen] = useState(false);

    const [, setCurrentTime] = useState(new Date());

    // Update time every second
    useEffect(() => {
        const timer = setInterval(() => setCurrentTime(new Date()), 1000);
        return () => clearInterval(timer);
    }, []);

    // Close sidebar when navigating on mobile
    const handleNavChange = (navInfo: string) => {
        setActiveNav(navInfo);
        setIsSidebarOpen(false);
    };

    useEffect(() => {
        let wsUrl: string;
        const apiUrl = import.meta.env.VITE_API_URL;

        if (apiUrl && apiUrl.startsWith('http')) {
            wsUrl = apiUrl.replace('http', 'ws') + '/ws';
        } else {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            wsUrl = `${protocol}//${window.location.host}/ws`;
        }

        const ws = new WebSocket(wsUrl);

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                if (data.type === 'system_status' || !data.type) {
                    // Default assumption for now if type missing, or explicit system_status
                    setStatus(prev => ({ ...prev, ...data }));
                }
                // SPX price and log handlers removed - not used in Dashboard
            } catch (e) {
                console.error("Parse error", e);
            }
        };

        ws.onclose = () => {
            setStatus(prev => ({ ...prev, backend_connected: false }));
        };

        ws.onopen = () => {
            setStatus(prev => ({ ...prev, backend_connected: true }));
        }

        return () => ws.close();
    }, []);

    const formatCurrency = useCallback((value: string | number, currency?: string) => {
        if (value === "N/A") return value;
        const num = typeof value === 'string' ? parseFloat(value.replace(/[^0-9.-]/g, '')) : value;
        if (isNaN(num)) return value;
        return new Intl.NumberFormat('en-US', {
            style: 'currency',
            currency: currency || status.account_currency || 'USD',
            minimumFractionDigits: 2
        }).format(num);
    }, [status.account_currency]);

    const systemStatuses = [
        {
            label: 'Backend API',
            connected: status.backend_connected,
            description: 'FastAPI WebSocket server'
        },
        {
            label: 'Redis Pub/Sub',
            connected: status.redis_connected,
            description: 'Real-time message broker'
        },
        {
            label: 'NautilusTrader',
            connected: status.nautilus_active,
            description: 'Event-driven trading engine'
        },
        {
            label: 'IB Gateway',
            connected: status.connected,
            description: status.account_id ? `Account: ${status.account_id}` : 'Interactive Brokers connection'
        }
    ];



    const handleLogout = async () => {
        try {
            await fetch('/logout', { method: 'POST' });
            window.location.href = '/login';
        } catch (error) {
            console.error('Logout failed:', error);
            window.location.href = '/login';
        }
    };

    return (
        <div className="min-h-screen bg-background">
            {/* Sidebar */}
            <Sidebar
                isOpen={isSidebarOpen}
                onClose={() => setIsSidebarOpen(false)}
                onLogout={handleLogout}
            >
                <SidebarItem
                    icon="lineChart"
                    label="Dashboard"
                    active={activeNav === 'dashboard'}
                    onClick={() => handleNavChange('dashboard')}
                />
                <SidebarItem
                    icon="cpu"
                    label="Strategies"
                    active={activeNav === 'strategies'}
                    onClick={() => handleNavChange('strategies')}
                />
                <SidebarItem
                    icon="barChart"
                    label="Analytics"
                    active={activeNav === 'analytics'}
                    onClick={() => handleNavChange('analytics')}
                />
                <SidebarItem
                    icon="activity"
                    label="Positions"
                    active={activeNav === 'positions'}
                    onClick={() => handleNavChange('positions')}
                />
                <SidebarItem
                    icon="server"
                    label="Logs"
                    active={activeNav === 'logs'}
                    onClick={() => handleNavChange('logs')}
                />
                <SidebarItem
                    icon="database"
                    label="VictoriaLogs"
                    onClick={() => window.open(`${window.location.origin}/vmui/`, '_blank')}
                />
            </Sidebar>

            {/* Main Content */}
            <main className="transition-all duration-300 md:ml-64 min-h-screen">
                <div className="p-4 md:p-8 space-y-6 md:space-y-8 max-w-[100vw] overflow-x-hidden">
                    {/* Header */}
                    <Header
                        title={activeNav === 'strategies' ? "Strategy Management" : activeNav === 'analytics' ? "Trade Analytics" : "Trader Dashboard"}
                        subtitle={activeNav === 'strategies' ? "Configure and control automated trading strategies" : activeNav === 'analytics' ? "Performance metrics and trade history" : "Real-time portfolio monitoring & trading analytics"}
                        onMenuClick={() => setIsSidebarOpen(true)}
                    />

                    {activeNav === 'strategies' ? (
                        <Strategies />
                    ) : activeNav === 'analytics' ? (
                        <Analytics />
                    ) : activeNav === 'logs' ? (
                        <LogViewer />
                    ) : (
                        <>
                            {/* Stats Grid */}
                            <section className="grid gap-6 md:grid-cols-2 lg:grid-cols-4">
                                <StatCard
                                    title="Net Liquidation"
                                    value={formatCurrency(status.net_liquidation)}
                                    icon="dollarSign"
                                    status="success"
                                    subtitle="Total account value"
                                />
                                <StatCard
                                    title="Day P&L"
                                    value={formatCurrency(status.day_realized_pnl || "0.0")}
                                    icon={(() => {
                                        const pnl = parseFloat((status.day_realized_pnl || "0.0").replace(/[^0-9.-]/g, ''));
                                        return pnl >= 0 ? "trendingUp" : "trendingDown";
                                    })()}
                                    status={(() => {
                                        const pnl = parseFloat((status.day_realized_pnl || "0.0").replace(/[^0-9.-]/g, ''));
                                        return pnl >= 0 ? "success" : "destructive";
                                    })()}
                                    subtitle="Realized P&L today"
                                />
                                <StatCard
                                    title="Buying Power"
                                    value={formatCurrency(status.buying_power)}
                                    icon="zap"
                                    status="info"
                                    subtitle="Available margin"
                                />
                                <StatCard
                                    title="Open Positions"
                                    value={status.open_positions.toString()}
                                    icon="activity"
                                    subtitle="Active trades"
                                />
                            </section>

                            {/* Charts and Status */}
                            <section className="grid gap-6 lg:grid-cols-3">
                                {/* Active Strategies Panel */}
                                <Card variant="glass" className="lg:col-span-2">
                                    <CardHeader className="flex flex-row items-center justify-between">
                                        <div>
                                            <CardTitle>Active Strategies</CardTitle>
                                            <p className="text-sm text-muted-foreground mt-1">
                                                Running strategies and their performance
                                            </p>
                                        </div>
                                        <div className="flex items-center gap-2">
                                            <Badge variant="success" pulse>
                                                Live
                                            </Badge>
                                        </div>
                                    </CardHeader>
                                    <CardContent>
                                        <div className="overflow-x-auto">
                                            <table className="w-full text-left text-sm">
                                                <thead className="border-b border-white/10 uppercase text-xs text-muted-foreground">
                                                    <tr>
                                                        <th className="px-4 py-3">Strategy</th>
                                                        <th className="px-4 py-3">Status</th>
                                                        <th className="px-4 py-3">Position</th>
                                                        <th className="px-4 py-3 text-right">Unrealized P&L</th>
                                                    </tr>
                                                </thead>
                                                <tbody className="divide-y divide-white/5">
                                                    {status.strategies && status.strategies.filter(s => s.running || (s.positions && s.positions.length > 0)).length > 0 ? (
                                                        status.strategies.filter(s => s.running || (s.positions && s.positions.length > 0)).map((strategy) => (
                                                            <tr key={strategy.id} className="hover:bg-white/5 transition-colors">
                                                                <td className="px-4 py-3 font-medium">
                                                                    <div className="flex items-center gap-2">
                                                                        <span>{strategy.config?.name || strategy.id}</span>
                                                                    </div>
                                                                </td>
                                                                <td className="px-4 py-3">
                                                                    <Badge variant={strategy.running ? "success" : "secondary"} className="text-[10px]">
                                                                        {strategy.status || (strategy.running ? "RUNNING" : "STOPPED")}
                                                                    </Badge>
                                                                </td>
                                                                <td className="px-4 py-3">
                                                                    {strategy.positions && strategy.positions.length > 0 ? (
                                                                        strategy.positions.map((pos, idx) => (
                                                                            <div key={idx} className="text-xs">
                                                                                <span className="font-semibold">{pos.symbol}</span>
                                                                                <span className="ml-1 text-muted-foreground">
                                                                                    ({pos.quantity > 0 ? '+' : ''}{pos.quantity})
                                                                                </span>
                                                                            </div>
                                                                        ))
                                                                    ) : (
                                                                        <span className="text-muted-foreground text-xs">Flat</span>
                                                                    )}
                                                                </td>
                                                                <td className="px-4 py-3 text-right">
                                                                    {strategy.positions && strategy.positions.length > 0 ? (
                                                                        strategy.positions.map((pos, idx) => (
                                                                            <div key={idx} className={`tabular-nums font-medium ${pos.unrealized_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                                                                                {formatCurrency(pos.unrealized_pnl)}
                                                                            </div>
                                                                        ))
                                                                    ) : (
                                                                        <span className="text-muted-foreground">-</span>
                                                                    )}
                                                                </td>
                                                            </tr>
                                                        ))
                                                    ) : (
                                                        <tr>
                                                            <td colSpan={4} className="px-4 py-8 text-center text-muted-foreground">
                                                                No active strategies
                                                            </td>
                                                        </tr>
                                                    )}
                                                </tbody>
                                            </table>
                                        </div>
                                    </CardContent>
                                </Card>

                                {/* System Status */}
                                <Card variant="glass">
                                    <CardHeader>
                                        <CardTitle>System Status</CardTitle>
                                        <p className="text-sm text-muted-foreground mt-1">
                                            Connection health monitoring
                                        </p>
                                    </CardHeader>
                                    <CardContent>
                                        <SystemStatusPanel statuses={systemStatuses} />
                                    </CardContent>
                                </Card>
                            </section>

                            {/* Open Positions Table */}
                            <section>
                                <Card variant="glass">
                                    <CardHeader className="flex flex-row items-center justify-between">
                                        <div>
                                            <CardTitle>Open Positions</CardTitle>
                                            <p className="text-sm text-muted-foreground mt-1">
                                                Real-time portfolio holdings
                                            </p>
                                        </div>
                                        <Badge variant="outline">
                                            {status.positions?.length || 0} Open
                                        </Badge>
                                    </CardHeader>
                                    <CardContent>
                                        <div className="overflow-x-auto">
                                            <table className="w-full text-left text-sm">
                                                <thead className="border-b border-white/10 uppercase text-xs text-muted-foreground">
                                                    <tr>
                                                        <th className="px-4 py-3">Symbol</th>
                                                        <th className="px-4 py-3 text-right">Qty</th>
                                                        <th className="px-4 py-3 text-right">Avg Price</th>
                                                        <th className="px-4 py-3 text-right">Unrealized P&L</th>
                                                    </tr>
                                                </thead>
                                                <tbody className="divide-y divide-white/5">
                                                    {status.positions && status.positions.length > 0 ? (
                                                        status.positions.map((pos) => (
                                                            <tr key={pos.symbol} className="hover:bg-white/5 transition-colors">
                                                                <td className="px-4 py-3 font-medium">{pos.symbol}</td>
                                                                <td className="px-4 py-3 text-right tabular-nums">{pos.quantity}</td>
                                                                <td className="px-4 py-3 text-right tabular-nums">
                                                                    {new Intl.NumberFormat('en-US', { style: 'currency', currency: status.account_currency }).format(pos.avg_price)}
                                                                </td>
                                                                <td className={`px-4 py-3 text-right tabular-nums font-medium ${pos.unrealized_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'
                                                                    }`}>
                                                                    {new Intl.NumberFormat('en-US', { style: 'currency', currency: status.account_currency, signDisplay: 'always' }).format(pos.unrealized_pnl)}
                                                                </td>
                                                            </tr>
                                                        ))
                                                    ) : (
                                                        <tr>
                                                            <td colSpan={4} className="px-4 py-8 text-center text-muted-foreground">
                                                                No open positions
                                                            </td>
                                                        </tr>
                                                    )}
                                                </tbody>
                                            </table>
                                        </div>
                                    </CardContent>
                                </Card>
                            </section>

                            {/* Recent Activity */}
                            <section>
                                <Card variant="glass">
                                    <CardHeader className="flex flex-row items-center justify-between">
                                        <div>
                                            <CardTitle>Recent Activity</CardTitle>
                                            <p className="text-sm text-muted-foreground mt-1">
                                                Latest trades (Current Session)
                                            </p>
                                        </div>
                                        <Badge variant="outline" className="border-cyan-500/20 text-cyan-400 bg-cyan-500/5">
                                            Live
                                        </Badge>
                                    </CardHeader>
                                    <CardContent>
                                        <div className="space-y-4">
                                            {/* Activity items */}
                                            {status.recent_trades && status.recent_trades.length > 0 ? (
                                                status.recent_trades.map((trade, index) => (
                                                    <ActivityItem
                                                        key={`${trade.symbol}-${index}`}
                                                        type={trade.type}
                                                        symbol={trade.symbol}
                                                        quantity={trade.quantity}
                                                        price={trade.price}
                                                        time={trade.time}
                                                        currency={status.account_currency}
                                                    />
                                                ))
                                            ) : (
                                                <div className="text-center py-8 text-muted-foreground text-sm">
                                                    No recent trades in current session
                                                </div>
                                            )}
                                        </div>
                                    </CardContent>
                                </Card>
                            </section>
                        </>
                    )}
                </div>
            </main>
        </div>
    );
}

interface ActivityItemProps {
    type: 'buy' | 'sell' | 'dividend';
    symbol: string;
    quantity?: number;
    price?: number;
    amount?: number;
    time: string;
    currency?: string;
}

function ActivityItem({ type, symbol, quantity, price, amount, time, currency }: ActivityItemProps) {
    const getTypeStyles = () => {
        switch (type) {
            case 'buy':
                return {
                    bg: 'bg-emerald-500/10',
                    text: 'text-emerald-400',
                    icon: Icons.trendingUp,
                    label: 'BUY'
                };
            case 'sell':
                return {
                    bg: 'bg-red-500/10',
                    text: 'text-red-400',
                    icon: Icons.trendingDown,
                    label: 'SELL'
                };
            case 'dividend':
                return {
                    bg: 'bg-purple-500/10',
                    text: 'text-purple-400',
                    icon: Icons.dollarSign,
                    label: 'DIV'
                };
        }
    };

    const styles = getTypeStyles();
    const IconComponent = styles.icon;

    const format = (value: number) => {
        return new Intl.NumberFormat('en-US', {
            style: 'currency',
            currency: currency || 'USD',
            minimumFractionDigits: 2
        }).format(value);
    };

    return (
        <div className="flex items-center justify-between p-4 rounded-xl bg-white/5 hover:bg-white/8 transition-colors">
            <div className="flex items-center gap-4">
                <div className={`p-3 rounded-xl ${styles.bg}`}>
                    <IconComponent className={`w-5 h-5 ${styles.text}`} />
                </div>
                <div>
                    <div className="flex items-center gap-2">
                        <span className="font-semibold">{symbol}</span>
                        <Badge variant={type === 'sell' ? 'destructive' : type === 'buy' ? 'success' : 'info'} className="text-[10px]">
                            {styles.label}
                        </Badge>
                    </div>
                    <p className="text-sm text-muted-foreground">
                        {type === 'dividend'
                            ? `Dividend received: ${format(amount || 0)}`
                            : `${quantity} shares @ ${format(price || 0)}`
                        }
                    </p>
                </div>
            </div>
            <div className="text-right">
                <p className={`font-semibold tabular-nums ${type === 'sell' ? 'text-red-400' : type === 'buy' ? 'text-emerald-400' : 'text-purple-400'}`}>
                    {type === 'dividend'
                        ? `+${format(amount || 0)}`
                        : type === 'buy'
                            ? `-${format((quantity || 0) * (price || 0))}`
                            : `+${format((quantity || 0) * (price || 0))}`
                    }
                </p>
                <p className="text-xs text-muted-foreground">{time}</p>
            </div>
        </div>
    );
}
