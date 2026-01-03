import { useState, useEffect, useRef } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from './ui/card';
import { Badge } from './ui/badge';
import { Icons } from './ui/icons';

interface StrategyStatus {
    name: string;
    is_active: boolean;
    current_price: number | null;
    status: {
        strategy_id?: string;
        instrument_id?: string;
        last_bid?: number | null;
        last_ask?: number | null;
        last_update?: string | null;
        is_running?: boolean;
        quote_tick_count?: number;
        trade_tick_count?: number;
        bar_count?: number;
        data_count?: number;
        has_instrument?: boolean;
    };
    logs: string[];
}

interface StrategyPanelProps {
    strategy?: StrategyStatus;
}

export function StrategyPanel({ strategy }: StrategyPanelProps) {
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const logContainerRef = useRef<HTMLDivElement>(null);
    const [autoScroll, setAutoScroll] = useState(true);
    const [mockPrice, setMockPrice] = useState("5950.50");

    // Auto-scroll logs to bottom when new logs arrive
    useEffect(() => {
        if (autoScroll && logContainerRef.current) {
            logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
        }
    }, [strategy?.logs, autoScroll]);

    const handleStart = async () => {
        setIsLoading(true);
        setError(null);
        try {
            const apiUrl = import.meta.env.VITE_API_URL || '';
            const response = await fetch(`${apiUrl}/strategy/start`, {
                method: 'POST',
            });
            const result = await response.json();
            if (!result.success) {
                setError(result.error || 'Failed to start strategy');
            }
        } catch (err) {
            setError(`Error: ${err instanceof Error ? err.message : 'Unknown error'}`);
        } finally {
            setIsLoading(false);
        }
    };

    const handleStop = async () => {
        setIsLoading(true);
        setError(null);
        try {
            const apiUrl = import.meta.env.VITE_API_URL || '';
            const response = await fetch(`${apiUrl}/strategy/stop`, {
                method: 'POST',
            });
            const result = await response.json();
            if (!result.success) {
                setError(result.error || 'Failed to stop strategy');
            }
        } catch (err) {
            setError(`Error: ${err instanceof Error ? err.message : 'Unknown error'}`);
        } finally {
            setIsLoading(false);
        }
    };

    const handleMockTick = async () => {
        setError(null);
        try {
            const price = parseFloat(mockPrice);
            if (isNaN(price)) {
                setError('Invalid price value');
                return;
            }
            const apiUrl = import.meta.env.VITE_API_URL || '';
            const response = await fetch(`${apiUrl}/strategy/mock-tick?price=${price}`, {
                method: 'POST',
            });
            const result = await response.json();
            if (!result.success) {
                setError(result.error || 'Failed to send mock tick');
            }
        } catch (err) {
            setError(`Error: ${err instanceof Error ? err.message : 'Unknown error'}`);
        }
    };

    const isActive = strategy?.is_active ?? false;
    const currentPrice = strategy?.current_price;
    const logs = strategy?.logs ?? [];
    const status = strategy?.status ?? {};

    // Format price for display
    const formatPrice = (price: number | null | undefined) => {
        if (price === null || price === undefined) return 'N/A';
        return price.toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        });
    };

    // Get log entry styling based on level
    const getLogStyle = (log: string) => {
        if (log.includes('[ERROR]')) return 'text-red-400';
        if (log.includes('[WARNING]')) return 'text-yellow-400';
        if (log.includes('[DEBUG]')) return 'text-gray-500';
        if (log.includes('[MANAGER]')) return 'text-cyan-400';
        if (log.includes('RECEIVED')) return 'text-emerald-400';
        if (log.includes('SENDING')) return 'text-blue-400';
        if (log.includes('MOCK DATA')) return 'text-purple-400';
        return 'text-gray-300';
    };

    return (
        <Card variant="glass">
            <CardHeader className="flex flex-row items-center justify-between">
                <div>
                    <CardTitle className="flex items-center gap-3">
                        <div className={`p-2 rounded-lg ${isActive ? 'bg-emerald-500/20' : 'bg-gray-500/20'}`}>
                            <Icons.target className={`w-5 h-5 ${isActive ? 'text-emerald-400' : 'text-gray-400'}`} />
                        </div>
                        <span>{strategy?.name || 'SPX 0DTE Opening Straddle'}</span>
                    </CardTitle>
                    <p className="text-sm text-muted-foreground mt-2 ml-12">
                        0DTE Opening Straddle strategy for SPX index
                    </p>
                </div>
                <div className="flex items-center gap-3">
                    <Badge
                        variant={isActive ? 'success' : 'outline'}
                        pulse={isActive}
                        className="min-w-[80px] justify-center"
                    >
                        {isActive ? 'Active' : 'Inactive'}
                    </Badge>
                </div>
            </CardHeader>

            <CardContent className="space-y-6">
                {/* Price Display */}
                <div className="grid gap-4 md:grid-cols-2">
                    {/* Current Price - Main Display */}
                    <div className="p-5 rounded-xl bg-gradient-to-br from-cyan-500/10 to-blue-500/10 border border-cyan-500/20">
                        <div className="flex items-center justify-between">
                            <div>
                                <p className="text-xs text-muted-foreground uppercase tracking-wider">SPX Index Price</p>
                                <p className="text-3xl font-bold text-cyan-400 tabular-nums mt-1">
                                    ${formatPrice(currentPrice)}
                                </p>
                                <p className="text-xs text-muted-foreground mt-1">
                                    {status.instrument_id || 'SPX.CBOE'}
                                </p>
                                <p className="text-xs text-muted-foreground mt-2">
                                    Last Price (indices don't have bid/ask)
                                </p>
                            </div>
                            <div className="p-3 rounded-xl bg-cyan-500/10">
                                <Icons.dollarSign className="w-8 h-8 text-cyan-400" />
                            </div>
                        </div>
                    </div>

                    {/* Last Update Time */}
                    <div className="p-5 rounded-xl bg-white/5 border border-white/10">
                        <div className="flex flex-col justify-center h-full">
                            <p className="text-xs text-muted-foreground uppercase tracking-wider mb-2">Last Update</p>
                            {status.last_update ? (
                                <>
                                    <p className="text-2xl font-bold tabular-nums">
                                        {new Date(status.last_update).toLocaleTimeString()}
                                    </p>
                                    <p className="text-xs text-muted-foreground mt-1">
                                        {new Date(status.last_update).toLocaleDateString()}
                                    </p>
                                </>
                            ) : (
                                <p className="text-lg text-muted-foreground">
                                    Waiting for data...
                                </p>
                            )}
                        </div>
                    </div>
                </div>

                {/* Data Counters */}
                <div className="grid gap-3 grid-cols-4">
                    <div
                        className="p-3 rounded-lg bg-white/5 text-center cursor-help transition-colors hover:bg-white/10"
                        title="5-second price bars aggregated from market data. More stable than individual ticks."
                    >
                        <p className="text-2xl font-bold tabular-nums">{status.bar_count ?? 0}</p>
                        <p className="text-xs text-muted-foreground flex items-center justify-center gap-1">
                            Bars
                            <span className="text-[10px] opacity-50">ⓘ</span>
                        </p>
                    </div>
                    <div
                        className="p-3 rounded-lg bg-white/5 text-center cursor-help transition-colors hover:bg-white/10"
                        title="Quote ticks with bid/ask prices. For indices, this is typically the Last Price (calculated index value)."
                    >
                        <p className="text-2xl font-bold tabular-nums">{status.quote_tick_count ?? 0}</p>
                        <p className="text-xs text-muted-foreground flex items-center justify-center gap-1">
                            Quotes
                            <span className="text-[10px] opacity-50">ⓘ</span>
                        </p>
                    </div>
                    <div
                        className="p-3 rounded-lg bg-white/5 text-center cursor-help transition-colors hover:bg-white/10"
                        title="Individual trade executions. Indices typically send Last Price updates as trade ticks."
                    >
                        <p className="text-2xl font-bold tabular-nums">{status.trade_tick_count ?? 0}</p>
                        <p className="text-xs text-muted-foreground flex items-center justify-center gap-1">
                            Trades
                            <span className="text-[10px] opacity-50">ⓘ</span>
                        </p>
                    </div>
                    <div
                        className="p-3 rounded-lg bg-white/5 text-center cursor-help transition-colors hover:bg-white/10"
                        title="Custom data types not recognized as standard bars/ticks. Used for debugging unusual data from broker."
                    >
                        <p className="text-2xl font-bold tabular-nums">{status.data_count ?? 0}</p>
                        <p className="text-xs text-muted-foreground flex items-center justify-center gap-1">
                            Custom
                            <span className="text-[10px] opacity-50">ⓘ</span>
                        </p>
                    </div>
                </div>

                {/* Control Buttons */}
                <div className="flex gap-4">
                    <button
                        onClick={handleStart}
                        disabled={isLoading || isActive}
                        className={`
                            flex-1 flex items-center justify-center gap-2 px-6 py-3 rounded-xl font-semibold
                            transition-all duration-200
                            ${isActive || isLoading
                                ? 'bg-gray-700/50 text-gray-500 cursor-not-allowed'
                                : 'bg-gradient-to-r from-emerald-600 to-emerald-500 hover:from-emerald-500 hover:to-emerald-400 text-white shadow-lg shadow-emerald-500/20 hover:shadow-emerald-500/30'
                            }
                        `}
                    >
                        <Icons.play className="w-5 h-5" />
                        {isLoading && !isActive ? 'Starting...' : 'Start Strategy'}
                    </button>
                    <button
                        onClick={handleStop}
                        disabled={isLoading || !isActive}
                        className={`
                            flex-1 flex items-center justify-center gap-2 px-6 py-3 rounded-xl font-semibold
                            transition-all duration-200
                            ${!isActive || isLoading
                                ? 'bg-gray-700/50 text-gray-500 cursor-not-allowed'
                                : 'bg-gradient-to-r from-red-600 to-red-500 hover:from-red-500 hover:to-red-400 text-white shadow-lg shadow-red-500/20 hover:shadow-red-500/30'
                            }
                        `}
                    >
                        <Icons.stop className="w-5 h-5" />
                        {isLoading && isActive ? 'Stopping...' : 'Stop Strategy'}
                    </button>
                </div>

                {/* Mock Test Section (for testing while market is closed) */}
                {isActive && (
                    <div className="p-4 rounded-xl bg-purple-500/5 border border-purple-500/20">
                        <div className="flex items-center gap-2 mb-3">
                            <div className="p-1.5 rounded-lg bg-purple-500/20">
                                <Icons.zap className="w-4 h-4 text-purple-400" />
                            </div>
                            <span className="text-sm font-medium text-purple-300">Mock Test Mode</span>
                            <Badge variant="outline" className="text-xs border-purple-500/30 text-purple-400 bg-purple-500/10">
                                Dev Only
                            </Badge>
                        </div>
                        <p className="text-xs text-muted-foreground mb-3">
                            Send simulated price data to test the strategy while the market is closed.
                        </p>
                        <div className="flex gap-3">
                            <input
                                type="number"
                                value={mockPrice}
                                onChange={(e) => setMockPrice(e.target.value)}
                                step="0.01"
                                className="flex-1 px-4 py-2 rounded-lg bg-black/30 border border-white/10 text-white font-mono text-sm focus:outline-none focus:border-purple-500/50"
                                placeholder="Enter mock price..."
                            />
                            <button
                                onClick={handleMockTick}
                                className="px-4 py-2 rounded-lg bg-gradient-to-r from-purple-600 to-purple-500 hover:from-purple-500 hover:to-purple-400 text-white font-semibold transition-all duration-200 flex items-center gap-2"
                            >
                                <Icons.zap className="w-4 h-4" />
                                Send Mock Tick
                            </button>
                        </div>
                    </div>
                )}

                {/* Error Display */}
                {error && (
                    <div className="flex items-center gap-3 p-4 rounded-xl bg-red-500/10 border border-red-500/20">
                        <Icons.alertCircle className="w-5 h-5 text-red-400 flex-shrink-0" />
                        <p className="text-sm text-red-400">{error}</p>
                    </div>
                )}

                {/* Strategy Logs */}
                <div className="space-y-3">
                    <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2">
                            <Icons.fileText className="w-4 h-4 text-muted-foreground" />
                            <h4 className="text-sm font-medium">Strategy Log</h4>
                            <Badge variant="outline" className="text-xs">
                                {logs.length} entries
                            </Badge>
                        </div>
                        <div className="flex items-center gap-2">
                            <label className="flex items-center gap-2 text-xs text-muted-foreground cursor-pointer">
                                <input
                                    type="checkbox"
                                    checked={autoScroll}
                                    onChange={(e) => setAutoScroll(e.target.checked)}
                                    className="rounded border-white/20 bg-white/5"
                                />
                                Auto-scroll
                            </label>
                        </div>
                    </div>

                    <div
                        ref={logContainerRef}
                        className="h-64 overflow-y-auto rounded-xl bg-black/30 border border-white/10 p-4 font-mono text-xs"
                    >
                        {logs.length === 0 ? (
                            <div className="flex items-center justify-center h-full text-muted-foreground">
                                <p>No log entries yet. Start the strategy to see logs.</p>
                            </div>
                        ) : (
                            <div className="space-y-1">
                                {logs.map((log, index) => (
                                    <div key={index} className={`${getLogStyle(log)} leading-relaxed`}>
                                        {log}
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                </div>
            </CardContent>
        </Card>
    );
}
