import { useState, useEffect, useRef } from 'react';
import { Icons } from './ui/icons';

interface LogEntry {
    timestamp: string;
    step: string;
    message: string;
    data: Record<string, any>;
    level: 'info' | 'warning' | 'error' | 'success';
}

interface StrategyLogTerminalProps {
    logs: LogEntry[];
    onClear?: () => void;
    className?: string;
}

const stepColors: Record<string, string> = {
    CONNECTION_CHECK: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
    DATA_SUBSCRIPTION: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
    CHAIN_SCAN: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
    MOCK_EXECUTION: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
    ORDER_SIMULATION: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
    EXIT_SIMULATION: 'bg-rose-500/20 text-rose-400 border-rose-500/30',
    TEST_COMPLETE: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
};

const levelIcons: Record<string, keyof typeof Icons> = {
    info: 'activity',
    warning: 'zap',
    error: 'wifiOff',
    success: 'trendingUp',
};

const levelColors: Record<string, string> = {
    info: 'text-slate-400',
    warning: 'text-amber-400',
    error: 'text-red-400',
    success: 'text-emerald-400',
};

export function StrategyLogTerminal({ logs, onClear, className = '' }: StrategyLogTerminalProps) {
    const scrollRef = useRef<HTMLDivElement>(null);
    const [autoScroll, setAutoScroll] = useState(true);

    useEffect(() => {
        if (autoScroll && scrollRef.current) {
            scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
        }
    }, [logs, autoScroll]);

    const formatTime = (timestamp: string) => {
        const date = new Date(timestamp);
        return date.toLocaleTimeString('en-US', { hour12: false });
    };

    return (
        <div className={`flex flex-col bg-black/40 border border-white/10 rounded-xl overflow-hidden ${className}`}>
            {/* Terminal Header */}
            <div className="flex items-center justify-between px-4 py-2 bg-white/5 border-b border-white/10">
                <div className="flex items-center gap-2">
                    <div className="flex gap-1.5">
                        <div className="w-3 h-3 rounded-full bg-red-500/80" />
                        <div className="w-3 h-3 rounded-full bg-amber-500/80" />
                        <div className="w-3 h-3 rounded-full bg-emerald-500/80" />
                    </div>
                    <span className="text-xs font-medium text-muted-foreground ml-2">Strategy Logs</span>
                </div>
                <div className="flex items-center gap-2">
                    <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
                        <input
                            type="checkbox"
                            checked={autoScroll}
                            onChange={(e) => setAutoScroll(e.target.checked)}
                            className="rounded border-white/20 bg-white/5"
                        />
                        Auto-scroll
                    </label>
                    {onClear && (
                        <button
                            onClick={onClear}
                            className="text-xs text-muted-foreground hover:text-white transition-colors"
                        >
                            Clear
                        </button>
                    )}
                </div>
            </div>

            {/* Terminal Content */}
            <div
                ref={scrollRef}
                className="flex-1 overflow-y-auto p-4 font-mono text-sm space-y-2 max-h-80"
            >
                {logs.length === 0 ? (
                    <div className="text-muted-foreground text-center py-8">
                        No logs yet. Click "Test Strategy" to run a dry run.
                    </div>
                ) : (
                    logs.map((log, index) => {
                        const IconComponent = Icons[levelIcons[log.level] || 'activity'];
                        const stepColorClass = stepColors[log.step] || 'bg-slate-500/20 text-slate-400 border-slate-500/30';

                        return (
                            <div key={index} className="flex items-start gap-2 group">
                                {/* Timestamp */}
                                <span className="text-muted-foreground text-xs shrink-0 pt-0.5">
                                    {formatTime(log.timestamp)}
                                </span>

                                {/* Level Icon */}
                                <IconComponent className={`w-4 h-4 shrink-0 mt-0.5 ${levelColors[log.level]}`} />

                                {/* Step Badge */}
                                <span className={`text-xs px-2 py-0.5 rounded border shrink-0 ${stepColorClass}`}>
                                    {log.step}
                                </span>

                                {/* Message */}
                                <span className={`${levelColors[log.level]} break-words`}>
                                    {log.message}
                                </span>
                            </div>
                        );
                    })
                )}
            </div>
        </div>
    );
}

interface StrategyStatusPanelProps {
    runtime: {
        positions_opened?: boolean;
        entry_underlying_price?: number | null;
        call_exit_target?: number | null;
        put_exit_target?: number | null;
        current_spx_price?: number;
        distance_to_call_exit?: number | null;
        distance_to_put_exit?: number | null;
        call_closed?: boolean;
        put_closed?: boolean;
    };
}

export function StrategyStatusPanel({ runtime }: StrategyStatusPanelProps) {
    if (!runtime.positions_opened) {
        return (
            <div className="p-4 rounded-xl bg-white/5 border border-white/10 text-center text-muted-foreground text-sm">
                No active position. Strategy will trigger at 09:30 EST.
            </div>
        );
    }

    return (
        <div className="p-4 rounded-xl bg-white/5 border border-white/10 space-y-4">
            <h4 className="text-sm font-medium text-muted-foreground uppercase tracking-wide">
                Live Position Status
            </h4>

            <div className="grid grid-cols-2 gap-4">
                {/* Entry Price */}
                <div className="space-y-1">
                    <span className="text-xs text-muted-foreground">Entry Price</span>
                    <div className="text-lg font-medium text-white">
                        ${runtime.entry_underlying_price?.toFixed(2) || '—'}
                    </div>
                </div>

                {/* Current Price */}
                <div className="space-y-1">
                    <span className="text-xs text-muted-foreground">Current SPX</span>
                    <div className="text-lg font-medium text-cyan-400">
                        ${runtime.current_spx_price?.toFixed(2) || '—'}
                    </div>
                </div>
            </div>

            {/* Exit Targets */}
            <div className="grid grid-cols-2 gap-4 pt-2 border-t border-white/10">
                {/* Call Exit */}
                <div className={`space-y-2 p-3 rounded-lg ${runtime.call_closed ? 'bg-emerald-500/10 border border-emerald-500/20' : 'bg-rose-500/10 border border-rose-500/20'}`}>
                    <div className="flex items-center justify-between">
                        <span className="text-xs font-medium">CALL Exit</span>
                        {runtime.call_closed && (
                            <span className="text-xs text-emerald-400">CLOSED</span>
                        )}
                    </div>
                    <div className="text-sm">
                        Target: <span className="text-rose-400 font-medium">${runtime.call_exit_target?.toFixed(2) || '—'}</span>
                    </div>
                    {!runtime.call_closed && runtime.distance_to_call_exit !== null && (
                        <div className="text-xs text-muted-foreground">
                            Distance: <span className={runtime.distance_to_call_exit! <= 1 ? 'text-rose-400' : 'text-white'}>
                                {runtime.distance_to_call_exit! > 0 ? '+' : ''}{runtime.distance_to_call_exit?.toFixed(2)} pts
                            </span>
                        </div>
                    )}
                </div>

                {/* Put Exit */}
                <div className={`space-y-2 p-3 rounded-lg ${runtime.put_closed ? 'bg-emerald-500/10 border border-emerald-500/20' : 'bg-blue-500/10 border border-blue-500/20'}`}>
                    <div className="flex items-center justify-between">
                        <span className="text-xs font-medium">PUT Exit</span>
                        {runtime.put_closed && (
                            <span className="text-xs text-emerald-400">CLOSED</span>
                        )}
                    </div>
                    <div className="text-sm">
                        Target: <span className="text-blue-400 font-medium">${runtime.put_exit_target?.toFixed(2) || '—'}</span>
                    </div>
                    {!runtime.put_closed && runtime.distance_to_put_exit !== null && (
                        <div className="text-xs text-muted-foreground">
                            Distance: <span className={runtime.distance_to_put_exit! <= 1 ? 'text-blue-400' : 'text-white'}>
                                {runtime.distance_to_put_exit! > 0 ? '+' : ''}{runtime.distance_to_put_exit?.toFixed(2)} pts
                            </span>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
