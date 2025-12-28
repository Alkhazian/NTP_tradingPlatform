import { cn } from '../../lib/utils';
import { Card, CardContent, CardHeader, CardTitle } from './card';
import { Icons } from './icons';
import type { IconName } from './icons';
import type { LucideIcon } from 'lucide-react';

interface StatCardProps {
    title: string;
    value: string | number;
    subtitle?: string;
    icon?: IconName;
    trend?: {
        value: number;
        isPositive: boolean;
    };
    status?: 'success' | 'warning' | 'destructive' | 'info';
    className?: string;
    loading?: boolean;
}

export function StatCard({
    title,
    value,
    subtitle,
    icon,
    trend,
    status,
    className,
    loading = false
}: StatCardProps) {
    const IconComponent: LucideIcon | undefined = icon ? Icons[icon] : undefined;

    return (
        <Card variant="stat" className={cn("group", className)}>
            {/* Gradient accent line */}
            <div className={cn(
                "absolute top-0 left-0 right-0 h-[2px] opacity-80",
                status === 'success' && "bg-gradient-to-r from-emerald-500 to-cyan-500",
                status === 'destructive' && "bg-gradient-to-r from-red-500 to-orange-500",
                status === 'warning' && "bg-gradient-to-r from-amber-500 to-yellow-500",
                status === 'info' && "bg-gradient-to-r from-blue-500 to-purple-500",
                !status && "bg-gradient-to-r from-cyan-500 to-blue-500"
            )} />

            <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle size="sm" className="uppercase tracking-wider text-xs">
                    {title}
                </CardTitle>
                {IconComponent && (
                    <div className={cn(
                        "p-2 rounded-lg transition-all duration-300",
                        "bg-white/5 group-hover:bg-white/10",
                        status === 'success' && "text-emerald-400",
                        status === 'destructive' && "text-red-400",
                        status === 'warning' && "text-amber-400",
                        status === 'info' && "text-cyan-400",
                        !status && "text-muted-foreground"
                    )}>
                        <IconComponent className="w-4 h-4" />
                    </div>
                )}
            </CardHeader>

            <CardContent>
                {loading ? (
                    <div className="h-8 w-24 rounded bg-white/5 shimmer" />
                ) : (
                    <div className="flex items-end gap-3">
                        <span className={cn(
                            "text-2xl font-bold tabular-nums tracking-tight",
                            status === 'success' && "text-emerald-400",
                            status === 'destructive' && "text-red-400",
                            !status && "text-foreground"
                        )}>
                            {value}
                        </span>

                        {trend && (
                            <div className={cn(
                                "flex items-center text-sm font-medium mb-0.5",
                                trend.isPositive ? "text-emerald-400" : "text-red-400"
                            )}>
                                {trend.isPositive ? (
                                    <Icons.chevronUp className="w-4 h-4" />
                                ) : (
                                    <Icons.chevronDown className="w-4 h-4" />
                                )}
                                <span>{Math.abs(trend.value)}%</span>
                            </div>
                        )}
                    </div>
                )}

                {subtitle && (
                    <p className="text-xs text-muted-foreground mt-1.5">
                        {subtitle}
                    </p>
                )}
            </CardContent>
        </Card>
    );
}
