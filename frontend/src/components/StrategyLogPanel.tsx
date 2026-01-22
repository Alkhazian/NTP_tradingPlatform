// Strategy Log Panel - Live logs for a specific strategy
// Polls the API every 5 seconds, auto-scrolls to bottom

import { useEffect, useState, useRef } from 'react';

interface LogEntry {
    _time: string;
    _msg: string;
    level: string;
    source?: string;
    component?: string;
    strategy_id?: string;
    [key: string]: unknown;
}

interface StrategyLogPanelProps {
    strategyId: string;
    maxLogs?: number;
    pollInterval?: number;
}

export function StrategyLogPanel({
    strategyId,
    maxLogs = 1000,
    pollInterval = 5000
    // start is now implicitly 12h ago in backend if not provided
}: StrategyLogPanelProps) {
    const [logs, setLogs] = useState<LogEntry[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const containerRef = useRef<HTMLDivElement>(null);

    // Fetch logs effect
    useEffect(() => {
        const fetchLogs = async () => {
            try {
                const resp = await fetch(`/api/logs/strategy/${strategyId}?limit=${maxLogs}`);

                if (!resp.ok) {
                    if (resp.status === 502) {
                        setError('Log service unavailable');
                    } else if (resp.status === 504) {
                        setError('Query timeout');
                    } else {
                        setError('Failed to fetch logs');
                    }
                    return;
                }

                const data = await resp.json();
                // Data comes Newest-First from API, so just use it directly.
                // This puts Newest at the top of the list.
                const newLogs = data.logs || [];
                setLogs(newLogs);
                setError(null);
            } catch (e) {
                console.error("Log fetch failed", e);
                setError('Network error');
            } finally {
                setLoading(false);
            }
        };

        fetchLogs();
        const interval = setInterval(fetchLogs, pollInterval);
        return () => clearInterval(interval);
    }, [strategyId, maxLogs, pollInterval]);

    const getLevelClass = (level: string) => {
        switch (level) {
            case 'ERROR':
            case 'CRITICAL':
                return 'text-red-400 font-semibold';
            case 'WARNING':
                return 'text-amber-400';
            case 'INFO':
                return 'text-gray-300';
            case 'DEBUG':
                return 'text-gray-500';
            default:
                return 'text-gray-400';
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

    if (loading && logs.length === 0) {
        return (
            <div className="h-full flex items-center justify-center text-xs text-gray-500">
                Loading logs...
            </div>
        );
    }

    return (
        <div
            ref={containerRef}
            className="h-full overflow-y-auto bg-black/40 font-mono text-xs p-2 space-y-0.5"
        >
            {error && (
                <div className="text-amber-400 text-center py-1 mb-1">
                    {error}
                </div>
            )}

            {logs.length === 0 && !error && (
                <div className="text-gray-500 italic text-center py-4">
                    No logs in the last hour
                </div>
            )}

            {logs.map((log, i) => (
                <div
                    key={`${log._time}-${i}`}
                    className="break-all border-b border-white/5 pb-0.5 last:border-0"
                >
                    <span className="text-gray-500 mr-2">
                        {formatTime(log._time)}
                    </span>
                    <span className={getLevelClass(log.level)}>
                        {log._msg}
                    </span>
                </div>
            ))}
        </div>
    );
}

export default StrategyLogPanel;
