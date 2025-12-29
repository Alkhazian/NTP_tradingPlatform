import { cn } from '../../lib/utils';
import { Badge } from './badge';
import { Icons } from './icons';

interface StatusIndicatorProps {
    label: string;
    connected: boolean;
    variant?: "success" | "warning" | "destructive" | "info";
    statusLabel?: string;
    description?: string;
    className?: string;
}

export function StatusIndicator({ label, connected, variant, statusLabel, description, className }: StatusIndicatorProps) {
    const finalVariant = variant || (connected ? "success" : "destructive");
    const finalLabel = statusLabel || (connected ? "Online" : "Offline");

    return (
        <div className={cn(
            "flex items-center justify-between p-3 rounded-lg",
            "bg-white/5 hover:bg-white/8 transition-colors duration-200",
            className
        )}>
            <div className="flex items-center gap-3">
                <div className={cn(
                    "p-2 rounded-lg",
                    finalVariant === "success" ? "bg-emerald-500/10" :
                        finalVariant === "destructive" ? "bg-red-500/10" :
                            finalVariant === "warning" ? "bg-amber-500/10" :
                                "bg-cyan-500/10"
                )}>
                    {connected ? (
                        <Icons.wifi className={cn("w-4 h-4",
                            finalVariant === "success" ? "text-emerald-400" :
                                finalVariant === "destructive" ? "text-red-400" :
                                    finalVariant === "warning" ? "text-amber-400" :
                                        "text-cyan-400"
                        )} />
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
                variant={finalVariant}
                pulse={finalVariant === "success" || finalVariant === "warning"}
            >
                {finalLabel}
            </Badge>
        </div>
    );
}

interface SystemStatusPanelProps {
    statuses: {
        label: string;
        connected: boolean;
        variant?: "success" | "warning" | "destructive" | "info";
        statusLabel?: string;
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
                        variant={status.variant}
                        statusLabel={status.statusLabel}
                        description={status.description}
                    />
                ))}
            </div>
        </div>
    );
}
