import { cn } from '../../lib/utils';
import { Icons } from '../ui/icons';

interface HeaderProps {
    title: string;
    subtitle?: string;
    className?: string;
}

export function Header({ title, subtitle, className }: HeaderProps) {
    const currentTime = new Date().toLocaleTimeString('en-US', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false
    });

    const currentDate = new Date().toLocaleDateString('en-US', {
        weekday: 'long',
        year: 'numeric',
        month: 'long',
        day: 'numeric'
    });

    return (
        <header className={cn(
            "flex flex-col md:flex-row md:items-center justify-between gap-4 mb-8",
            className
        )}>
            <div className="space-y-1">
                <h1 className="text-3xl md:text-4xl font-bold tracking-tight gradient-text">
                    {title}
                </h1>
                {subtitle && (
                    <p className="text-muted-foreground">{subtitle}</p>
                )}
            </div>

            <div className="flex items-center gap-4">
                {/* Live indicator */}
                <div className="flex items-center gap-2 px-4 py-2 rounded-full bg-white/5 border border-white/10">
                    <span className="relative flex h-2 w-2">
                        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                        <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
                    </span>
                    <span className="text-sm font-medium text-emerald-400">LIVE</span>
                </div>

                {/* Time display */}
                <div className="hidden sm:flex items-center gap-3 px-4 py-2 rounded-xl bg-white/5 border border-white/10">
                    <Icons.clock className="w-4 h-4 text-muted-foreground" />
                    <div className="text-right">
                        <p className="text-sm font-mono font-semibold tabular-nums">{currentTime}</p>
                        <p className="text-xs text-muted-foreground">{currentDate}</p>
                    </div>
                </div>
            </div>
        </header>
    );
}

interface SidebarItemProps {
    icon: keyof typeof Icons;
    label: string;
    active?: boolean;
    onClick?: () => void;
}

export function SidebarItem({ icon, label, active, onClick }: SidebarItemProps) {
    const IconComponent = Icons[icon];

    return (
        <button
            onClick={onClick}
            className={cn(
                "w-full flex items-center gap-3 px-4 py-3 rounded-xl transition-all duration-200",
                "hover:bg-white/10 group",
                active && "bg-gradient-to-r from-cyan-500/20 to-blue-500/20 border border-cyan-500/30"
            )}
        >
            <div className={cn(
                "p-2 rounded-lg transition-colors",
                active ? "bg-cyan-500/20 text-cyan-400" : "bg-white/5 text-muted-foreground group-hover:text-foreground"
            )}>
                <IconComponent className="w-4 h-4" />
            </div>
            <span className={cn(
                "font-medium text-sm",
                active ? "text-foreground" : "text-muted-foreground group-hover:text-foreground"
            )}>
                {label}
            </span>
        </button>
    );
}

interface SidebarProps {
    className?: string;
    children?: React.ReactNode;
}

export function Sidebar({ className, children }: SidebarProps) {
    return (
        <aside className={cn(
            "w-64 glass-sidebar flex flex-col h-screen fixed left-0 top-0 z-50",
            className
        )}>
            {/* Logo */}
            <div className="p-6 border-b border-white/10">
                <div className="flex items-center gap-3">
                    <div className="p-2 rounded-xl bg-gradient-to-br from-cyan-500 to-blue-600">
                        <Icons.activity className="w-6 h-6 text-white" />
                    </div>
                    <div>
                        <h2 className="font-bold text-lg">NTD Trader</h2>
                        <p className="text-xs text-muted-foreground">Dashboard v1.0</p>
                    </div>
                </div>
            </div>

            {/* Navigation */}
            <nav className="flex-1 p-4 space-y-2 overflow-y-auto">
                {children}
            </nav>

            {/* Footer */}
            <div className="p-4 border-t border-white/10">
                <div className="flex items-center gap-3 px-4 py-3 rounded-xl bg-white/5">
                    <div className="w-8 h-8 rounded-full bg-gradient-to-br from-purple-500 to-pink-500 flex items-center justify-center text-sm font-bold">
                        T
                    </div>
                    <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium truncate">Trader</p>
                        <p className="text-xs text-muted-foreground">Paper Trading</p>
                    </div>
                </div>
            </div>
        </aside>
    );
}
