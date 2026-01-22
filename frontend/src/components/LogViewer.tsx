// System Log Viewer - Global logs from VictoriaLogs API
// Polls every 5 seconds, with filtering and level selection

import { useEffect, useState, useCallback } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from './ui/card';
import { Badge } from './ui/badge';
import { Icons } from './ui/icons';

interface LogEntry {
    _time: string;
    _msg: string;
    level: string;
    source: string;
    strategy_id?: string;
    component?: string;
    [key: string]: unknown;
}

export default function LogViewer() {
    const [logs, setLogs] = useState<LogEntry[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [filters, setFilters] = useState({
        search: '',
        level: '',
        source: '',
        strategy_id: '',
    });
    // Removed logsEndRef for auto-scroll

    const fetchLogs = useCallback(async () => {
        setLoading(true);

        try {
            const params = new URLSearchParams();
            if (filters.level) params.set('level', filters.level);
            if (filters.source) params.set('source', filters.source);
            if (filters.search) params.set('search', filters.search);
            params.set('limit', '1000');
            // Filter out noisy Nginx logs by default
            params.append('exclude_containers', 'ntd-nginx');

            const resp = await fetch(`/api/logs/tail?${params}`);

            if (!resp.ok) {
                if (resp.status === 502) {
                    setError('Log service unavailable');
                } else if (resp.status === 504) {
                    setError('Query timed out');
                } else {
                    setError('Failed to fetch logs');
                }
                return;
            }

            const data = await resp.json();
            // Reverse to show Newest at the Top
            setLogs((data.logs || []).reverse());
            setError(null);
        } catch (e) {
            console.error('Log fetch error:', e);
            setError('Network error');
        } finally {
            setLoading(false);
        }
    }, [filters]);

    // Poll every 5 seconds
    useEffect(() => {
        fetchLogs();
        const interval = setInterval(fetchLogs, 5000);
        return () => clearInterval(interval);
    }, [fetchLogs]);

    const getLevelColor = (level: string) => {
        switch (level) {
            case 'ERROR':
            case 'CRITICAL':
                return 'text-red-400 font-medium bg-red-500/5';
            case 'WARNING':
                return 'text-amber-400 bg-amber-500/5';
            case 'INFO':
                return 'text-blue-300';
            case 'DEBUG':
                return 'text-muted-foreground';
            default:
                return 'text-white/70';
        }
    };

    const formatTime = (ts: string) => {
        try {
            const date = new Date(ts);
            return date.toLocaleTimeString('en-US', {
                hour12: false,
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
            });
        } catch {
            return ts;
        }
    };

    return (
        <Card variant="glass" className="h-[calc(100vh-8rem)] flex flex-col overflow-hidden">
            <CardHeader className="flex flex-col md:flex-row md:items-center justify-between gap-4 shrink-0 pb-4">
                <div className="space-y-1">
                    <CardTitle className="text-xl">System Logs</CardTitle>
                    <p className="text-sm text-muted-foreground">
                        Live from VictoriaLogs (last 5 minutes)
                    </p>
                </div>

                <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-3 w-full md:w-auto">
                    {/* Level Filter */}
                    <select
                        value={filters.level}
                        onChange={(e) => setFilters(f => ({ ...f, level: e.target.value }))}
                        className="px-3 py-2 rounded-xl bg-white/5 border border-white/10 text-sm focus:outline-none focus:ring-1 focus:ring-primary/50"
                    >
                        <option value="">All Levels</option>
                        <option value="ERROR">ERROR</option>
                        <option value="WARNING">WARNING</option>
                        <option value="INFO">INFO</option>
                        <option value="DEBUG">DEBUG</option>
                    </select>

                    {/* Source Filter */}
                    <select
                        value={filters.source}
                        onChange={(e) => setFilters(f => ({ ...f, source: e.target.value }))}
                        className="px-3 py-2 rounded-xl bg-white/5 border border-white/10 text-sm focus:outline-none focus:ring-1 focus:ring-primary/50"
                    >
                        <option value="">All Sources</option>
                        <option value="strategy">Strategy</option>
                        <option value="system">System</option>
                        <option value="nautilus">Nautilus</option>
                        <option value="container">Container</option>
                    </select>

                    {/* Search Input */}
                    <div className="relative flex-1 sm:flex-none">
                        <Icons.search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                        <input
                            type="text"
                            placeholder="Search..."
                            value={filters.search}
                            onChange={(e) => setFilters(f => ({ ...f, search: e.target.value }))}
                            className="w-full sm:w-48 pl-9 pr-9 py-2 rounded-xl bg-white/5 border border-white/10 text-sm focus:outline-none focus:ring-1 focus:ring-primary/50 transition-all focus:bg-white/10"
                        />
                        {filters.search && (
                            <button
                                onClick={() => setFilters(f => ({ ...f, search: '' }))}
                                className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-white p-1"
                            >
                                <Icons.x className="w-3 h-3" />
                            </button>
                        )}
                    </div>

                    <div className="flex items-center gap-2 self-end sm:self-auto">
                        <Badge
                            variant={error ? "destructive" : "success"}
                            pulse={!error && !loading}
                            className="h-9 px-3"
                        >
                            {error ? "OFFLINE" : "LIVE"}
                        </Badge>

                        <button
                            className="h-9 w-9 flex items-center justify-center rounded-xl bg-white/5 border border-white/10 hover:bg-white/10 hover:text-cyan-400 transition-colors"
                            title="Refresh"
                            onClick={fetchLogs}
                        >
                            <Icons.refresh className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                        </button>
                    </div>
                </div>
            </CardHeader>

            <CardContent className="flex-1 min-h-0 p-0 relative bg-black/40 border-t border-white/5">
                {error && (
                    <div className="absolute top-0 left-0 right-0 bg-amber-500/10 text-amber-400 text-sm text-center py-2 z-10">
                        {error} - logs will resume when service is available
                    </div>
                )}

                <div className="absolute inset-0 p-4 font-mono text-xs overflow-auto md:p-6 custom-scrollbar">
                    {logs.length === 0 && !error && (
                        <div className="flex flex-col items-center justify-center h-full text-muted-foreground/50 space-y-2">
                            <div className="w-2 h-2 rounded-full bg-cyan-500 animate-ping" />
                            <p>No logs in the last 5 minutes</p>
                        </div>
                    )}

                    <div className="space-y-1">
                        {logs.map((log, i) => (
                            <div
                                key={`${log._time}-${i}`}
                                className={`whitespace-pre-wrap break-all px-2 py-0.5 rounded transition-colors hover:bg-white/5 ${getLevelColor(log.level)}`}
                            >
                                <span className="text-gray-500 mr-2">
                                    {formatTime(log._time)}
                                </span>
                                {log.strategy_id && (
                                    <span className="text-cyan-400 mr-2">
                                        [{log.strategy_id}]
                                    </span>
                                )}
                                <span>{log._msg}</span>
                            </div>
                        ))}
                    </div>

                </div>
            </CardContent>
        </Card>
    );
}
