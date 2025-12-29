import { useState, useMemo } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '../ui/card';


// Mock logs for now since we don't have websocket logs wired up yet
// In real app, these should come from context or a separate log websocket
const MOCK_LOGS = [
    { id: 1, timestamp: new Date().toISOString(), type: 'system', subtype: 'info', message: 'System initialized' },
    { id: 2, timestamp: new Date().toISOString(), type: 'connection', subtype: 'success', message: 'Connected to IB Gateway' },
    { id: 3, timestamp: new Date().toISOString(), type: 'strategy', subtype: 'info', message: 'DummyStrategy started' },
];

export default function LogViewer() {
    const [filterType, setFilterType] = useState<string>('all');
    const [strategyFilter, setStrategyFilter] = useState('');

    // In a real implementation, logs would be passed as props or from context
    const logs = MOCK_LOGS;

    const filteredLogs = useMemo(() => {
        let result = logs;
        if (filterType !== 'all') {
            result = result.filter(log => log.type === filterType);
        }
        if (filterType === 'strategy' && strategyFilter) {
            result = result.filter(log => log.message.toLowerCase().includes(strategyFilter.toLowerCase()));
        }
        return result;
    }, [logs, filterType, strategyFilter]);

    const getTypeColor = (type: string) => {
        switch (type) {
            case 'system': return 'text-blue-400';
            case 'trading': return 'text-emerald-400';
            case 'error': return 'text-red-400';
            default: return 'text-muted-foreground';
        }
    };

    return (
        <Card variant="glass" className="h-[600px] flex flex-col">
            <CardHeader className="flex flex-row items-center justify-between shrink-0">
                <CardTitle>System Logs</CardTitle>
                <div className="flex gap-2">
                    {['all', 'system', 'trading', 'strategy', 'connection'].map((type) => (
                        <button
                            key={type}
                            onClick={() => setFilterType(type)}
                            className={`px-3 py-1 text-xs rounded-full border transition-colors ${filterType === type
                                ? 'bg-white/10 border-white/20 text-white'
                                : 'border-transparent text-muted-foreground hover:text-white'
                                }`}
                        >
                            {type.charAt(0).toUpperCase() + type.slice(1)}
                        </button>
                    ))}
                </div>
                {filterType === 'strategy' && (
                    <div className="ml-4">
                        <input
                            type="text"
                            placeholder="Filter by strategy name..."
                            value={strategyFilter}
                            onChange={(e) => setStrategyFilter(e.target.value)}
                            className="bg-black/20 border border-white/10 rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-cyan-500"
                        />
                    </div>
                )}
            </CardHeader>
            <CardContent className="flex-1 overflow-hidden min-h-0">
                <div className="h-full overflow-y-auto space-y-1 font-mono text-sm">
                    {filteredLogs.map((log) => (
                        <div key={log.id} className="flex gap-4 p-2 hover:bg-white/5 rounded transition-colors group">
                            <span className="text-muted-foreground shrink-0 w-40 text-xs">
                                {new Date(log.timestamp).toLocaleTimeString()}
                            </span>
                            <span className={`shrink-0 w-24 font-semibold text-xs uppercase ${getTypeColor(log.type)}`}>
                                {log.type}
                            </span>
                            <span className="text-gray-300 group-hover:text-white transition-colors">
                                {log.message}
                            </span>
                        </div>
                    ))}
                </div>
            </CardContent>
        </Card>
    );
}
