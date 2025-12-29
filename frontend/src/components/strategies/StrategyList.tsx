import { useState } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '../ui/card';
import { Badge } from '../ui/badge';
import { useTrading, type StrategyStatus } from '../../context/TradingContext';

interface StrategyCardProps {
    strategy: StrategyStatus;
    onEnable: () => void;
    onDisable: (force?: boolean) => void;
    onPause: () => void;
    onResume: () => void;
    onUpdateConfig: (config: any) => void;
}

function StrategyCard({ strategy, onEnable, onDisable, onPause, onResume, onUpdateConfig }: StrategyCardProps) {
    const [isConfigOpen, setIsConfigOpen] = useState(false);
    const [configJson, setConfigJson] = useState(JSON.stringify(strategy.config, null, 2));

    const handleSaveConfig = () => {
        try {
            const parsed = JSON.parse(configJson);
            onUpdateConfig(parsed);
            setIsConfigOpen(false);
        } catch (e) {
            alert("Invalid JSON");
        }
    };

    const getStatusVariant = (status: string) => {
        switch (status) {
            case "RUNNING": return "success";
            case "PAUSED": return "warning";
            case "REDUCE_ONLY": return "warning";
            case "ERROR": return "destructive";
            case "STARTING":
            case "STOPPING": return "secondary";
            default: return "secondary";
        }
    };

    const getStatusLabel = (status: string) => {
        switch (status) {
            case "RUNNING": return "Running";
            case "PAUSED": return "Paused";
            case "REDUCE_ONLY": return "Reduce Only";
            case "ERROR": return "Error";
            case "STARTING": return "Starting...";
            case "STOPPING": return "Stopping...";
            default: return "Stopped";
        }
    };

    return (
        <Card variant="glass">
            <CardHeader className="flex flex-row items-center justify-between">
                <div>
                    <CardTitle className="flex items-center gap-2">
                        {strategy.name}
                        <Badge
                            variant={getStatusVariant(strategy.status)}
                            pulse={["RUNNING", "STARTING", "STOPPING"].includes(strategy.status)}
                        >
                            {getStatusLabel(strategy.status)}
                        </Badge>
                    </CardTitle>
                    <p className="text-sm text-muted-foreground mt-1">
                        {strategy.status === "ERROR" ? `Failed: ${strategy.error_count || 0} consecutive errors` :
                            strategy.status === "PAUSED" ? "Trading suspended, positions held" :
                                strategy.status === "REDUCE_ONLY" ? "Managing existing positions only" :
                                    "Configured Strategy"}
                    </p>
                </div>
                <div className="flex gap-2">
                    <button
                        onClick={() => setIsConfigOpen(!isConfigOpen)}
                        className="px-4 py-2 text-sm font-medium rounded-lg bg-white/5 hover:bg-white/10 transition-colors"
                    >
                        {isConfigOpen ? 'Close Config' : 'Configure'}
                    </button>

                    {/* Contextual Buttons Based on State */}
                    {strategy.status === "STOPPED" || strategy.status === "ERROR" ? (
                        <button
                            onClick={onEnable}
                            className="px-4 py-2 text-sm font-medium rounded-lg bg-emerald-500/10 text-emerald-400 hover:bg-emerald-400/20 transition-colors"
                        >
                            Start
                        </button>
                    ) : (
                        <>
                            {strategy.status === "RUNNING" && (
                                <button
                                    onClick={onPause}
                                    className="px-4 py-2 text-sm font-medium rounded-lg bg-yellow-500/10 text-yellow-500 hover:bg-yellow-500/20 transition-colors"
                                >
                                    Pause
                                </button>
                            )}
                            {(strategy.status === "PAUSED" || strategy.status === "REDUCE_ONLY") && (
                                <button
                                    onClick={onResume}
                                    className="px-4 py-2 text-sm font-medium rounded-lg bg-emerald-500/10 text-emerald-400 hover:bg-emerald-400/20 transition-colors"
                                >
                                    Resume
                                </button>
                            )}
                            <button
                                onClick={() => onDisable(false)}
                                className="px-4 py-2 text-sm font-medium rounded-lg bg-orange-500/10 text-orange-400 hover:bg-orange-500/20 transition-colors"
                            >
                                Stop
                            </button>
                            <button
                                onClick={() => onDisable(true)}
                                className="px-4 py-2 text-sm font-medium rounded-lg bg-red-500/20 text-red-500 hover:bg-red-500/30 transition-colors"
                                title="Emergency Stop: Close all positions immediately"
                            >
                                Force Stop
                            </button>
                        </>
                    )}
                </div>
            </CardHeader>
            <CardContent>
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
                    <div className="p-3 rounded-lg bg-white/5">
                        <p className="text-xs text-muted-foreground">PnL</p>
                        <p className={`font-bold ${strategy.pnl && strategy.pnl > 0 ? 'text-emerald-400' : strategy.pnl && strategy.pnl < 0 ? 'text-red-400' : ''}`}>
                            {strategy.pnl ? `$${strategy.pnl.toFixed(2)}` : '$0.00'}
                        </p>
                    </div>
                    {/* Add more metrics here if backend supports them */}
                </div>

                {isConfigOpen && (
                    <div className="mt-4 space-y-2">
                        <label className="text-sm font-medium">Configuration (JSON)</label>
                        <textarea
                            value={configJson}
                            onChange={(e) => setConfigJson(e.target.value)}
                            className="w-full h-40 bg-black/50 border border-white/10 rounded-lg p-3 font-mono text-sm focus:outline-none focus:border-cyan-500"
                        />
                        <button
                            onClick={handleSaveConfig}
                            className="w-full py-2 bg-cyan-500/10 text-cyan-400 rounded-lg hover:bg-cyan-500/20 transition-colors"
                        >
                            Save Changes
                        </button>
                    </div>
                )}
            </CardContent>
        </Card>
    );
}

export default function StrategyList() {
    const { status, enableStrategy, disableStrategy, pauseStrategy, resumeStrategy, stopAllStrategies, updateStrategyConfig } = useTrading();
    const strategies = status.strategies || [];

    return (
        <div className="space-y-6">
            <div className="flex justify-between items-center mb-4">
                <h3 className="text-lg font-semibold text-white/90">Strategies</h3>
                {strategies.some(s => s.status !== "STOPPED") && (
                    <button
                        onClick={() => {
                            if (window.confirm("EMERGENCY STOP: This will cancel ALL active orders and attempt to close ALL positions immediately. Are you sure?")) {
                                stopAllStrategies();
                            }
                        }}
                        className="px-4 py-2 text-sm font-bold rounded-lg bg-red-500 text-white hover:bg-red-600 transition-all shadow-lg hover:shadow-red-500/20"
                    >
                        EMERGENCY STOP ALL
                    </button>
                )}
            </div>

            {strategies.length === 0 ? (
                <div className="text-center py-12 text-muted-foreground border border-dashed border-white/10 rounded-xl">
                    No strategies loaded.
                </div>
            ) : (
                strategies.map((strategy) => (
                    <StrategyCard
                        key={strategy.name}
                        strategy={strategy}
                        onEnable={() => enableStrategy(strategy.name)}
                        onDisable={(force) => disableStrategy(strategy.name, force)}
                        onPause={() => pauseStrategy(strategy.name)}
                        onResume={() => resumeStrategy(strategy.name)}
                        onUpdateConfig={(config) => updateStrategyConfig(strategy.name, config)}
                    />
                ))
            )}
        </div>
    );
}
