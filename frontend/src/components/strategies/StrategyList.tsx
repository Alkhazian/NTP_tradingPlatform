import { useState } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '../ui/card';
import { Badge } from '../ui/badge';
import { useTrading, type StrategyStatus } from '../../context/TradingContext';

export default function StrategyList() {
    const { status, enableStrategy, disableStrategy, updateStrategyConfig } = useTrading();
    const strategies = status.strategies || [];

    return (
        <div className="space-y-6">
            <div className="flex justify-between items-center mb-4">
                <h3 className="text-lg font-semibold">Strategies</h3>
                {strategies.some(s => s.active) && (
                    <button
                        onClick={() => fetch('/api/strategies/stop_all', { method: 'POST' })}
                        className="px-4 py-2 text-sm font-medium rounded-lg bg-red-500/10 text-red-400 hover:bg-red-500/20 transition-colors"
                    >
                        Stop All Strategies
                    </button>
                )}
            </div>

            {strategies.length === 0 ? (
                <div className="text-center py-12 text-muted-foreground">
                    No strategies loaded.
                </div>
            ) : (
                strategies.map((strategy) => (
                    <StrategyCard
                        key={strategy.name}
                        strategy={strategy}
                        onEnable={() => enableStrategy(strategy.name)}
                        onDisable={() => disableStrategy(strategy.name)}
                        onUpdateConfig={(config) => updateStrategyConfig(strategy.name, config)}
                    />
                ))
            )}
        </div>
    );
}

interface StrategyCardProps {
    strategy: StrategyStatus;
    onEnable: () => void;
    onDisable: () => void;
    onUpdateConfig: (config: any) => void;
}

function StrategyCard({ strategy, onEnable, onDisable, onUpdateConfig }: StrategyCardProps) {
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

    return (
        <Card variant="glass">
            <CardHeader className="flex flex-row items-center justify-between">
                <div>
                    <CardTitle className="flex items-center gap-2">
                        {strategy.name}
                        <Badge variant={strategy.active ? "success" : "secondary"}>
                            {strategy.active ? "Active" : "Inactive"}
                        </Badge>
                    </CardTitle>
                    <p className="text-sm text-muted-foreground mt-1">
                        Configured Strategy
                    </p>
                </div>
                <div className="flex gap-2">
                    <button
                        onClick={() => setIsConfigOpen(!isConfigOpen)}
                        className="px-4 py-2 text-sm font-medium rounded-lg bg-white/5 hover:bg-white/10 transition-colors"
                    >
                        {isConfigOpen ? 'Close Config' : 'Configure'}
                    </button>
                    {strategy.active ? (
                        <button
                            onClick={onDisable}
                            className="px-4 py-2 text-sm font-medium rounded-lg bg-red-500/10 text-red-400 hover:bg-red-500/20 transition-colors"
                        >
                            Stop
                        </button>
                    ) : (
                        <button
                            onClick={onEnable}
                            className="px-4 py-2 text-sm font-medium rounded-lg bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 transition-colors"
                        >
                            Start
                        </button>
                    )}
                </div>
            </CardHeader>
            <CardContent>
                {/* Stats would go here if available in strategy stats */}
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
                    <div className="p-3 rounded-lg bg-white/5">
                        <p className="text-xs text-muted-foreground">PnL</p>
                        <p className={`font-bold ${strategy.pnl && strategy.pnl > 0 ? 'text-emerald-400' : strategy.pnl && strategy.pnl < 0 ? 'text-red-400' : ''}`}>
                            {strategy.pnl ? `$${strategy.pnl.toFixed(2)}` : '$0.00'}
                        </p>
                    </div>
                    <div className="p-3 rounded-lg bg-white/5">
                        <p className="text-xs text-muted-foreground">Trades</p>
                        <p className="font-bold">0</p>
                    </div>
                    <div className="p-3 rounded-lg bg-white/5">
                        <p className="text-xs text-muted-foreground">Win Rate</p>
                        <p className="font-bold">0%</p>
                    </div>
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
                            className="w-full py-2 bg-cyan-500/10 text-cyan-400 rounded-lg hover:bg-cyan-500/20"
                        >
                            Save Changes
                        </button>
                    </div>
                )}
            </CardContent>
        </Card>
    );
}
