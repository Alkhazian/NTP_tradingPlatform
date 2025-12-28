import { cn } from '../../lib/utils';
import { Badge } from './badge';
import { Icons } from './icons';

interface StatusIndicatorProps {
    label: string;
    connected: boolean;
    description?: string;
    className?: string;
}

export function StatusIndicator({ label, connected, description, className }: StatusIndicatorProps) {
    return (
        <div className={cn(
            "flex items-center justify-between p-3 rounded-lg",
            "bg-white/5 hover:bg-white/8 transition-colors duration-200",
            className
        )}>
            <div className="flex items-center gap-3">
                <div className={cn(
                    "p-2 rounded-lg",
                    connected ? "bg-emerald-500/10" : "bg-red-500/10"
                )}>
                    {connected ? (
                        <Icons.wifi className="w-4 h-4 text-emerald-400" />
                    ) : (
                        <Icons.wifiOff className="w-4 h-4 text-red-400" />
                    )}
                </div>
                <div>
                    <p className="text-sm font-medium">{label}</p>
                    {description && (
                        <p className="text-xs text-muted-foreground">{description}</p>
                    )}
                </div>
            </div>
            <Badge
                variant={connected ? "success" : "destructive"}
                pulse={connected}
            >
                {connected ? "Online" : "Offline"}
            </Badge>
        </div>
    );
}

interface SystemStatusPanelProps {
    statuses: {
        label: string;
        connected: boolean;
        description?: string;
    }[];
    className?: string;
}

export function SystemStatusPanel({ statuses, className }: SystemStatusPanelProps) {
    const allConnected = statuses.every(s => s.connected);
    const someConnected = statuses.some(s => s.connected);

    return (
        <div className={cn("space-y-3", className)}>
            <div className="flex items-center justify-between mb-4">
                <h3 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">
                    System Status
                </h3>
                <Badge
                    variant={allConnected ? "success" : someConnected ? "warning" : "destructive"}
                    pulse={allConnected}
                >
                    {allConnected ? "All Systems Go" : someConnected ? "Partial" : "Offline"}
                </Badge>
            </div>

            <div className="space-y-2">
                {statuses.map((status, index) => (
                    <StatusIndicator
                        key={index}
                        label={status.label}
                        connected={status.connected}
                        description={status.description}
                    />
                ))}
            </div>
        </div>
    );
}
