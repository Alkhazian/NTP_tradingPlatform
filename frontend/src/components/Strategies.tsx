
import { useState, useEffect } from 'react';
import { Card, CardContent } from './ui/card';
import { Badge } from './ui/badge';
import { Icons } from './ui/icons';

interface StrategyStatus {
    id: string;
    running: boolean;
    config: {
        id: string;
        name: string;
        enabled: boolean;
        instrument_id: string;
        strategy_type: string;
        order_size: number;
        parameters?: {
            [key: string]: any;
        };
    };
    metrics?: {
        total_trades: number;
        win_rate: number;
        realized_pnl: number;
        unrealized_pnl: number;
    };
}

export default function Strategies() {
    const [strategies, setStrategies] = useState<StrategyStatus[]>([]);
    const [editingId, setEditingId] = useState<string | null>(null);
    const [jsonEdit, setJsonEdit] = useState("");
    const [editError, setEditError] = useState<string | null>(null);

    const rawApiUrl = import.meta.env.VITE_API_URL || '';
    const apiUrl = rawApiUrl.endsWith('/') ? rawApiUrl.slice(0, -1) : rawApiUrl;

    const fetchStrategies = async () => {
        try {
            const res = await fetch(`${apiUrl}/strategies`);
            if (res.ok) {
                const data = await res.json();
                setStrategies(data);
            }
        } catch (error) {
            console.error("Failed to fetch strategies", error);
        }
    };

    useEffect(() => {
        fetchStrategies();
        const interval = setInterval(fetchStrategies, 5000);
        return () => clearInterval(interval);
    }, []);

    const handleStart = async (id: string) => {
        try {
            await fetch(`${apiUrl}/strategies/${id}/start`, { method: 'POST' });
            fetchStrategies();
        } catch (error) {
            console.error("Failed to start strategy", error);
        }
    };

    const handleStop = async (id: string) => {
        try {
            await fetch(`${apiUrl}/strategies/${id}/stop`, { method: 'POST' });
            fetchStrategies();
        } catch (error) {
            console.error("Failed to stop strategy", error);
        }
    };

    const startEditing = (strategy: StrategyStatus) => {
        setEditingId(strategy.id);
        setJsonEdit(JSON.stringify(strategy.config, null, 4));
        setEditError(null);
    };

    const cancelEditing = () => {
        setEditingId(null);
        setEditError(null);
    };

    const saveConfig = async (id: string) => {
        try {
            // Validate JSON
            let parsed;
            try {
                parsed = JSON.parse(jsonEdit);
            } catch (e) {
                setEditError("Invalid JSON syntax");
                return;
            }

            const res = await fetch(`${apiUrl}/strategies/${id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(parsed)
            });

            if (res.ok) {
                setEditingId(null);
                fetchStrategies();
            } else {
                const errorData = await res.json();
                setEditError(errorData.detail || "Failed to update strategy");
            }
        } catch (error) {
            console.error("Failed to update strategy", error);
            setEditError("Network error");
        }
    };

    return (
        <div className="space-y-6">
            <div className="flex items-center justify-between">
                <div>
                    <h2 className="text-2xl font-bold tracking-tight">Strategy Management</h2>
                    <p className="text-muted-foreground">Configure and control automated trading strategies</p>
                </div>
            </div>

            {/* Strategy List */}
            <div className="grid gap-6">
                {strategies.length === 0 ? (
                    <Card variant="glass">
                        <CardContent className="py-12 text-center text-muted-foreground">
                            No strategies loaded.
                            <br />
                            <span className="text-xs">Strategies are loaded from backend configuration.</span>
                        </CardContent>
                    </Card>
                ) : (
                    strategies
                        .filter(s => s.id !== 'spx-streamer-01') // Hide system actors
                        .map((strategy) => (
                            <Card key={strategy.id} variant="glass" className="overflow-hidden border-t-2 border-t-cyan-500/20">
                                {editingId === strategy.id ? (
                                    <div className="p-6 space-y-4">
                                        <div className="flex items-center justify-between">
                                            <h3 className="text-lg font-bold">Edit Configuration: {strategy.config.name}</h3>
                                            <div className="flex gap-2">
                                                <button onClick={cancelEditing} className="px-3 py-1 text-sm bg-white/5 hover:bg-white/10 rounded">Cancel</button>
                                                <button onClick={() => saveConfig(strategy.id)} className="px-3 py-1 text-sm bg-cyan-500 hover:bg-cyan-600 text-black font-bold rounded">Save JSON</button>
                                            </div>
                                        </div>
                                        <div className="space-y-2">
                                            <textarea
                                                value={jsonEdit}
                                                onChange={(e) => setJsonEdit(e.target.value)}
                                                className="w-full h-80 px-3 py-2 bg-black/40 border border-white/10 rounded font-mono text-xs focus:border-cyan-500/50 outline-none"
                                                placeholder="Paste strategy configuration JSON..."
                                            />
                                            {editError && <p className="text-xs text-red-400 font-medium">{editError}</p>}
                                        </div>
                                    </div>
                                ) : (
                                    <div className="p-6 flex flex-col gap-6">
                                        <div className="flex flex-col md:flex-row items-center justify-between gap-6">
                                            <div className="flex items-center gap-4">
                                                <div className={`p-4 rounded-xl ${strategy.running ? 'bg-emerald-500/10' : 'bg-white/5'}`}>
                                                    <Icons.cpu className={`w-8 h-8 ${strategy.running ? 'text-emerald-400' : 'text-muted-foreground'}`} />
                                                </div>
                                                <div>
                                                    <div className="flex items-center gap-2">
                                                        <h3 className="text-lg font-bold text-white">{strategy.config.name || strategy.id}</h3>
                                                        <Badge variant={strategy.running ? 'success' : 'secondary'}>
                                                            {strategy.running ? 'RUNNING' : 'STOPPED'}
                                                        </Badge>
                                                    </div>
                                                    <div className="mt-1 flex items-center gap-4 text-xs font-medium text-muted-foreground uppercase tracking-wider">
                                                        <span className="text-cyan-400/80">{strategy.config.strategy_type}</span>
                                                        <span>•</span>
                                                        <span>{strategy.config.instrument_id}</span>
                                                        <span>•</span>
                                                        <span className="text-emerald-400/80">Size: {strategy.config.order_size}</span>
                                                    </div>
                                                </div>
                                            </div>

                                            <div className="flex items-center gap-3">
                                                <button
                                                    onClick={() => startEditing(strategy)}
                                                    className="p-2 text-muted-foreground hover:text-white transition-colors bg-white/5 rounded-lg"
                                                    title="Edit Configuration"
                                                >
                                                    <Icons.settings className="w-5 h-5" />
                                                </button>

                                                {strategy.running ? (
                                                    <button
                                                        onClick={() => handleStop(strategy.id)}
                                                        className="flex items-center gap-2 px-6 py-2 bg-red-500/10 hover:bg-red-500/20 text-red-400 font-bold rounded-lg border border-red-500/30 transition-all active:scale-95"
                                                    >
                                                        <Icons.square className="w-4 h-4 fill-current" />
                                                        STOP
                                                    </button>
                                                ) : (
                                                    <button
                                                        onClick={() => handleStart(strategy.id)}
                                                        className="flex items-center gap-2 px-6 py-2 bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-400 font-bold rounded-lg border border-emerald-500/30 transition-all active:scale-95"
                                                    >
                                                        <Icons.play className="w-4 h-4 fill-current" />
                                                        START
                                                    </button>
                                                )}
                                            </div>
                                        </div>

                                        {/* Metrics Grid */}
                                        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 p-4 bg-white/5 rounded-xl border border-white/5">
                                            <div className="space-y-1">
                                                <p className="text-[10px] text-muted-foreground uppercase font-bold tracking-widest">Total Trades</p>
                                                <p className="text-xl font-bold">{strategy.metrics?.total_trades || 0}</p>
                                            </div>
                                            <div className="space-y-1">
                                                <p className="text-[10px] text-muted-foreground uppercase font-bold tracking-widest">Win Rate</p>
                                                <p className={`text-xl font-bold ${(strategy.metrics?.win_rate || 0) > 50 ? 'text-emerald-400' : 'text-white'}`}>
                                                    {strategy.metrics?.win_rate?.toFixed(1) || '0.0'}%
                                                </p>
                                            </div>
                                            <div className="space-y-1">
                                                <p className="text-[10px] text-muted-foreground uppercase font-bold tracking-widest">Realized PnL</p>
                                                <p className={`text-xl font-bold ${(strategy.metrics?.realized_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                                                    ${strategy.metrics?.realized_pnl?.toFixed(2) || '0.00'}
                                                </p>
                                            </div>
                                            <div className="space-y-1">
                                                <p className="text-[10px] text-muted-foreground uppercase font-bold tracking-widest">Unrealized PnL</p>
                                                <p className={`text-xl font-bold ${(strategy.metrics?.unrealized_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                                                    ${strategy.metrics?.unrealized_pnl?.toFixed(2) || '0.00'}
                                                </p>
                                            </div>
                                        </div>
                                    </div>
                                )}
                            </Card>
                        ))
                )}
            </div>
        </div>
    );
}
