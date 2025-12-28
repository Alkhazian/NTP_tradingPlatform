import { useEffect, useState, useRef } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from './ui/card';
import { Badge } from './ui/badge';
import { createChart, ColorType } from 'lightweight-charts';

interface SystemStatus {
    connected: boolean; // IB Gateway Connection
    net_liquidation: string;
    account_id: string | null;
    redis_connected: boolean;
    backend_connected: boolean;
}

export default function Dashboard() {
    const [status, setStatus] = useState<SystemStatus>({
        connected: false,
        net_liquidation: "N/A",
        account_id: null,
        redis_connected: false,
        backend_connected: false
    });
    const chartContainerRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        // Connect to WebSocket using the environment variable or default to window location
        let wsUrl: string;
        const apiUrl = import.meta.env.VITE_API_URL;

        if (apiUrl && apiUrl.startsWith('http')) {
            wsUrl = apiUrl.replace('http', 'ws') + '/ws';
        } else {
            // If relative URL (like '/') or undefined, use current window protocol/host
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            wsUrl = `${protocol}//${window.location.host}/ws`;
        }

        const ws = new WebSocket(wsUrl);

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                setStatus(prev => ({ ...prev, ...data }));
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

    useEffect(() => {
        if (chartContainerRef.current) {
            const chart = createChart(chartContainerRef.current, {
                layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: 'black' },
                width: chartContainerRef.current.clientWidth,
                height: 300,
            });
            const newSeries = chart.addAreaSeries({ lineColor: '#2962FF', topColor: '#2962FF', bottomColor: 'rgba(41, 98, 255, 0.28)' });
            newSeries.setData([
                { time: '2018-12-22', value: 32.51 },
                { time: '2018-12-23', value: 31.11 },
                { time: '2018-12-24', value: 27.02 },
                { time: '2018-12-25', value: 27.32 },
                { time: '2018-12-26', value: 25.17 },
                { time: '2018-12-27', value: 28.89 },
                { time: '2018-12-28', value: 25.46 },
                { time: '2018-12-29', value: 23.92 },
                { time: '2018-12-30', value: 22.68 },
                { time: '2018-12-31', value: 22.67 },
            ]);

            const handleResize = () => {
                chart.applyOptions({ width: chartContainerRef.current?.clientWidth || 500 });
            };
            window.addEventListener('resize', handleResize);
            return () => {
                window.removeEventListener('resize', handleResize);
                chart.remove();
            };
        }
    }, []);

    return (
        <div className="p-8 space-y-8 animate-in fade-in duration-500 bg-background min-h-screen">
            <h1 className="text-3xl font-bold tracking-tight">Trader Dashboard</h1>

            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
                <Card>
                    <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                        <CardTitle className="text-sm font-medium">Backend Status</CardTitle>
                    </CardHeader>
                    <CardContent>
                        <div className="flex items-center space-x-2">
                            <Badge variant={status.backend_connected ? "default" : "destructive"}>
                                {status.backend_connected ? "Connected" : "Disconnected"}
                            </Badge>
                        </div>
                    </CardContent>
                </Card>

                <Card>
                    <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                        <CardTitle className="text-sm font-medium">Redis Pub/Sub</CardTitle>
                    </CardHeader>
                    <CardContent>
                        <Badge variant={status.redis_connected ? "default" : "destructive"}>
                            {status.redis_connected ? "Active" : "Inactive"}
                        </Badge>
                    </CardContent>
                </Card>

                <Card>
                    <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                        <CardTitle className="text-sm font-medium">IB Gateway</CardTitle>
                    </CardHeader>
                    <CardContent>
                        <Badge variant={status.connected ? "default" : "destructive"}>
                            {status.connected ? "Active" : "Disconnected"}
                        </Badge>
                        {status.account_id && <div className="text-xs text-muted-foreground mt-1">ID: {status.account_id}</div>}
                    </CardContent>
                </Card>

                <Card>
                    <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                        <CardTitle className="text-sm font-medium">Net Liquidation</CardTitle>
                    </CardHeader>
                    <CardContent>
                        <div className="text-2xl font-bold">{status.net_liquidation}</div>
                    </CardContent>
                </Card>
            </div>

            <Card className="col-span-4">
                <CardHeader>
                    <CardTitle>Market Data (Preview)</CardTitle>
                </CardHeader>
                <CardContent>
                    <div ref={chartContainerRef} className="w-full h-[300px]" />
                </CardContent>
            </Card>
        </div>
    );
}
