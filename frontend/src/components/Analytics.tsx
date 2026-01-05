import { useState } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from './ui/card';
import { Badge } from './ui/badge';

interface Log {
    timestamp: number;
    message: string;
    level: string;
}

interface AnalyticsProps {
    spxPrice: number;
    spxLogs: Log[];
    isStreaming: boolean;
}

export default function Analytics({ spxPrice, spxLogs, isStreaming }: AnalyticsProps) {
    const [isLoading, setIsLoading] = useState(false);

    const API_URL = import.meta.env.VITE_API_URL || '';

    const handleToggle = async () => {
        setIsLoading(true);
        try {
            const endpoint = isStreaming ? '/analytics/spx/stop' : '/analytics/spx/start';
            const url = API_URL.endsWith('/') ? `${API_URL.slice(0, -1)}${endpoint}` : `${API_URL}${endpoint}`;

            const res = await fetch(url, {
                method: 'POST',
            });

            if (res.ok) {
                // Determine new state based on action assuming success usually means toggle
                // But better to verify response if API returned status.
                // Our API returns { status: "started" | "stopped" }
                // The WebSocket will update the actual status prop shortly
            }
        } catch (error) {
            console.error(error);
        } finally {
            setIsLoading(false);
        }
    };

    return (
        <div className="space-y-6 animate-in fade-in duration-500">
            {/* SPX Market Streaming Block */}
            <Card variant="glass" className="overflow-hidden relative">
                <div className="absolute top-0 right-0 p-4 opacity-50">
                    <div className="w-32 h-32 bg-cyan-500/10 rounded-full blur-3xl -mr-16 -mt-16" />
                </div>

                <CardHeader className="flex flex-row items-center justify-between">
                    <div>
                        <CardTitle className="text-xl">SPX Market Streaming</CardTitle>
                        <p className="text-sm text-muted-foreground mt-1">Real-time data feed from Interactive Brokers</p>
                    </div>
                    <Badge variant={isStreaming ? "success" : "outline"} className="uppercase tracking-wider">
                        {isStreaming ? "Streaming Active" : "Stream Offline"}
                    </Badge>
                </CardHeader>

                <CardContent>
                    <div className="flex items-center justify-between mt-4">
                        <div className="flex flex-col">
                            <span className="text-sm text-muted-foreground uppercase tracking-wider mb-1">Current SPX Price</span>
                            <div className="text-4xl font-bold font-mono tracking-tight text-white flex items-baseline gap-2">
                                {spxPrice > 0 ? (
                                    <>
                                        {spxPrice.toFixed(2)}
                                        <span className="text-sm font-normal text-muted-foreground">USD</span>
                                    </>
                                ) : (
                                    <span className="text-muted-foreground">---.--</span>
                                )}
                            </div>
                        </div>

                        <button
                            onClick={handleToggle}
                            disabled={isLoading}
                            className={`
                                relative px-8 py-3 rounded-xl font-medium transition-all duration-300
                                ${isStreaming
                                    ? 'bg-red-500/10 text-red-400 hover:bg-red-500/20 border border-red-500/20 shadow-[0_0_20px_rgba(239,68,68,0.1)]'
                                    : 'bg-cyan-500/10 text-cyan-400 hover:bg-cyan-500/20 border border-cyan-500/20 shadow-[0_0_20px_rgba(6,182,212,0.1)]'
                                }
                                disabled:opacity-50 disabled:cursor-not-allowed
                            `}
                        >
                            {isLoading ? (
                                <span className="flex items-center gap-2">
                                    <span className="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin" />
                                    Processing...
                                </span>
                            ) : (
                                isStreaming ? 'Stop Streaming' : 'Start Streaming'
                            )}
                        </button>
                    </div>
                </CardContent>
            </Card>

            {/* Logs Console */}
            <Card variant="glass" className="flex-1 min-h-[400px]">
                <CardHeader>
                    <CardTitle className="text-lg">Data Actor Logs</CardTitle>
                </CardHeader>
                <CardContent>
                    <div className="h-[400px] rounded-xl bg-black/40 border border-white/5 p-4 overflow-y-auto font-mono text-xs space-y-1.5 scrollbar-thin scrollbar-thumb-white/10 scrollbar-track-transparent">
                        {spxLogs.length > 0 ? (
                            spxLogs.map((log, i) => (
                                <div key={i} className="flex gap-3 hover:bg-white/5 p-1 rounded transition-colors group">
                                    <span className="text-gray-500 shrink-0 select-none">
                                        {new Date(log.timestamp / 1_000_000).toLocaleTimeString()}
                                    </span>
                                    <span className={`break-all ${log.level === 'error' ? 'text-red-400' :
                                        log.level === 'warning' ? 'text-orange-400' :
                                            'text-emerald-400/80'
                                        }`}>
                                        <span className="text-white/40 mr-2 group-hover:text-white/60 transition-colors">$</span>
                                        {log.message}
                                    </span>
                                </div>
                            ))
                        ) : (
                            <div className="h-full flex flex-col items-center justify-center text-muted-foreground/50 italic">
                                <span className="mb-2">No activity recorded</span>
                                <span className="text-xs">Start streaming to see real-time events</span>
                            </div>
                        )}
                    </div>
                </CardContent>
            </Card>
        </div>
    );
}
