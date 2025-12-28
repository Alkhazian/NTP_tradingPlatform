import {
    Activity,
    TrendingUp,
    TrendingDown,
    DollarSign,
    Zap,
    Database,
    Server,
    Wifi,
    WifiOff,
    ChevronUp,
    ChevronDown,
    LineChart,
    BarChart3,
    Clock,
    RefreshCw
} from 'lucide-react';

export const Icons = {
    activity: Activity,
    trendingUp: TrendingUp,
    trendingDown: TrendingDown,
    dollarSign: DollarSign,
    zap: Zap,
    database: Database,
    server: Server,
    wifi: Wifi,
    wifiOff: WifiOff,
    chevronUp: ChevronUp,
    chevronDown: ChevronDown,
    lineChart: LineChart,
    barChart: BarChart3,
    clock: Clock,
    refresh: RefreshCw,
};

export type IconName = keyof typeof Icons;
