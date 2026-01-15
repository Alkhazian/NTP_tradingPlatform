import { useEffect, useState, useRef } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from './ui/card';
import { Badge } from './ui/badge';
import { Icons } from './ui/icons';

export default function LogViewer() {
    const [logs, setLogs] = useState<string[]>([]);
    const [connected, setConnected] = useState(false);
    const [filter, setFilter] = useState('');
    const logsEndRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        let wsUrl: string;
        const apiUrl = import.meta.env.VITE_API_URL;

        if (apiUrl && apiUrl.startsWith('http')) {
            wsUrl = apiUrl.replace('http', 'ws');
            if (!wsUrl.endsWith('/')) wsUrl += '/';
            wsUrl += 'ws/logs';
        } else {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            let host = window.location.host;
            // If we are on Vite dev port, we likely want to connect to Nginx on port 80
            if (host.includes(':5173')) {
                host = window.location.hostname; // Default to port 80/443
            }
            wsUrl = `${protocol}//${host}/ws/logs`;
        }

        const ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            setConnected(true);
        };

        ws.onmessage = (event) => {
            setLogs(prev => {
                const newLogs = [...prev, event.data];
                // Keep only last 1000 lines to prevent memory issues
                if (newLogs.length > 1000) {
                    return newLogs.slice(-1000);
                }
                return newLogs;
            });
        };

        ws.onclose = () => {
            setConnected(false);
        };

        return () => ws.close();
    }, []);

    // Auto-scroll to bottom
    useEffect(() => {
        logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }, [logs, filter]);

    const filteredLogs = logs.filter(log =>
        log.toLowerCase().includes(filter.toLowerCase())
    );

    return (
        <Card variant="glass" className="h-[calc(100vh-8rem)] flex flex-col overflow-hidden">
            <CardHeader className="flex flex-col md:flex-row md:items-center justify-between gap-4 shrink-0 pb-4">
                <div className="space-y-1">
                    <CardTitle className="text-xl">System Logs</CardTitle>
                    <p className="text-sm text-muted-foreground">
                        Live stream from backend (logs/app.log)
                    </p>
                </div>

                <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-3 w-full md:w-auto">
                    {/* Search Input */}
                    <div className="relative flex-1 sm:flex-none">
                        <Icons.search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                        <input
                            type="text"
                            placeholder="Filter logs..."
                            value={filter}
                            onChange={(e) => setFilter(e.target.value)}
                            className="w-full sm:w-64 md:w-72 pl-9 pr-9 py-2 rounded-xl bg-white/5 border border-white/10 text-sm focus:outline-none focus:ring-1 focus:ring-primary/50 transition-all focus:bg-white/10"
                        />
                        {filter && (
                            <button
                                onClick={() => setFilter('')}
                                className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-white p-1"
                            >
                                <Icons.x className="w-3 h-3" />
                            </button>
                        )}
                    </div>

                    <div className="flex items-center gap-2 self-end sm:self-auto">
                        <Badge variant={connected ? "success" : "destructive"} pulse={connected} className="h-9 px-3">
                            {connected ? "LIVE" : "DISCONNECTED"}
                        </Badge>

                        <button
                            className="h-9 w-9 flex items-center justify-center rounded-xl bg-white/5 border border-white/10 hover:bg-white/10 hover:text-red-400 transition-colors"
                            title="Clear Logs"
                            onClick={() => setLogs([])}
                        >
                            <Icons.trash className="w-4 h-4" />
                        </button>
                    </div>
                </div>
            </CardHeader>

            <CardContent className="flex-1 min-h-0 p-0 relative bg-black/40 border-t border-white/5">
                <div className="absolute inset-0 p-4 font-mono text-xs overflow-auto md:p-6 custom-scrollbar">
                    {filteredLogs.length === 0 && filter && (
                        <div className="flex flex-col items-center justify-center h-full text-muted-foreground space-y-2">
                            <Icons.search className="w-8 h-8 opacity-20" />
                            <p>No logs matching "{filter}"</p>
                        </div>
                    )}

                    {filteredLogs.length === 0 && !filter && (
                        <div className="flex flex-col items-center justify-center h-full text-muted-foreground/50 space-y-2">
                            <div className="w-2 h-2 rounded-full bg-cyan-500 animate-ping" />
                            <p>Waiting for logs...</p>
                        </div>
                    )}

                    <div className="space-y-1">
                        {filteredLogs.map((log, i) => {
                            // Basic syntax highlighting
                            let colorClass = "text-white/70";
                            if (log.includes("ERROR") || log.includes("CRITICAL")) colorClass = "text-red-400 font-medium bg-red-500/5";
                            else if (log.includes("WARN")) colorClass = "text-amber-400 bg-amber-500/5";
                            else if (log.includes("INFO")) colorClass = "text-blue-300";
                            else if (log.includes("DEBUG")) colorClass = "text-muted-foreground";

                            return (
                                <div key={i} className={`whitespace-pre-wrap break-all px-2 py-0.5 rounded transition-colors hover:bg-white/5 ${colorClass}`}>
                                    {log}
                                </div>
                            );
                        })}
                    </div>
                    <div ref={logsEndRef} />
                </div>
            </CardContent>
        </Card>
    );
}
