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
        <Card variant="glass" className="h-[calc(100vh-8rem)] flex flex-col">
            <CardHeader className="flex flex-row items-center justify-between shrink-0">
                <div>
                    <CardTitle>System Logs</CardTitle>
                    <p className="text-sm text-muted-foreground mt-1">
                        Live stream from backend (logs/app.log)
                    </p>
                </div>
                <div className="flex items-center gap-2">
                    <div className="relative">
                        <Icons.search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                        <input
                            type="text"
                            placeholder="Filter logs..."
                            value={filter}
                            onChange={(e) => setFilter(e.target.value)}
                            className="pl-9 pr-4 py-2 rounded-lg bg-white/5 border border-white/10 text-sm focus:outline-none focus:ring-1 focus:ring-primary/50 w-64"
                        />
                        {filter && (
                            <button
                                onClick={() => setFilter('')}
                                className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-white"
                            >
                                <Icons.plus className="w-4 h-4 rotate-45" />
                            </button>
                        )}
                    </div>
                    <Badge variant={connected ? "success" : "destructive"} pulse={connected}>
                        {connected ? "Live" : "Disconnected"}
                    </Badge>
                    <div className="p-2 rounded-lg bg-white/5 border border-white/10 cursor-pointer hover:bg-white/10 transition-colors" title="Clear Logs" onClick={() => setLogs([])}>
                        <Icons.trash className="w-4 h-4 text-muted-foreground" />
                    </div>
                </div>
            </CardHeader>
            <CardContent className="flex-1 min-h-0 p-0 relative">
                <div className="absolute inset-0 p-4 font-mono text-xs overflow-auto bg-black/40 rounded-b-xl">
                    {filteredLogs.length === 0 && filter && (
                        <div className="text-center text-muted-foreground py-8">
                            No logs matching "{filter}"
                        </div>
                    )}
                    {filteredLogs.map((log, i) => (
                        <div key={i} className="whitespace-pre-wrap break-all text-white/80 hover:bg-white/5 px-2 py-0.5 rounded">
                            {log}
                        </div>
                    ))}
                    <div ref={logsEndRef} />
                </div>
            </CardContent>
        </Card>
    );
}
