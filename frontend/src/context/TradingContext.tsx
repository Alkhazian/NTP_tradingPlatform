import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';

// Types (should ideally be in a shared types file)
export interface SystemStatus {
    connected: boolean;
    nautilus_active: boolean;
    net_liquidation: string;
    buying_power: string;
    account_currency: string;
    account_id: string | null;
    redis_connected: boolean;
    backend_connected: boolean;
    open_positions: number;
    positions?: Position[];
    day_realized_pnl?: string;
    strategies?: StrategyStatus[];
    // Portfolio metrics
    margin_used?: string;
    margin_available?: string;
    margin_usage_percent?: string;
    total_unrealized_pnl?: string;
    total_realized_pnl?: string;
    net_exposure?: string;
    leverage?: string;
    recent_trades?: Trade[];
}

export interface Position {
    symbol: string;
    quantity: number;
    avg_price: number;
    unrealized_pnl: number;
}

export interface Trade {
    type: 'buy' | 'sell';
    symbol: string;
    quantity: number;
    price: number;
    time: string;
    timestamp: number;
}

export interface StrategyStatus {
    name: string;
    active: boolean;
    status: string; // "RUNNING", "REDUCE_ONLY", "STOPPED", etc.
    config: Record<string, any>;
    pnl?: number;
    error_count?: number;
}

interface TradingContextType {
    status: SystemStatus;
    enableStrategy: (name: string) => Promise<void>;
    disableStrategy: (name: string, force?: boolean) => Promise<void>;
    pauseStrategy: (name: string) => Promise<void>;
    resumeStrategy: (name: string) => Promise<void>;
    stopAllStrategies: () => Promise<void>;
    updateStrategyConfig: (name: string, config: any) => Promise<void>;
}

const TradingContext = createContext<TradingContextType | undefined>(undefined);

export const useTrading = () => {
    const context = useContext(TradingContext);
    if (!context) {
        throw new Error('useTrading must be used within a TradingProvider');
    }
    return context;
};

export const TradingProvider = ({ children }: { children: ReactNode }) => {
    const [status, setStatus] = useState<SystemStatus>({
        connected: false,
        nautilus_active: false,
        net_liquidation: "N/A",
        buying_power: "N/A",
        account_currency: "USD",
        account_id: null,
        redis_connected: false,
        backend_connected: false,
        open_positions: 0,
        positions: [],
        day_realized_pnl: "0.0 USD",
        strategies: [],
        recent_trades: []
    });

    useEffect(() => {
        let wsUrl: string;
        const apiUrl = import.meta.env.VITE_API_URL;

        if (apiUrl && apiUrl.startsWith('http')) {
            wsUrl = apiUrl.replace('http', 'ws') + '/ws';
        } else {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            wsUrl = `${protocol}//${window.location.host}/ws`;
        }

        const connectWs = () => {
            const ws = new WebSocket(wsUrl);

            ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);

                    // Transform strategies from object to array format
                    // Backend: { "DummyStrategy": { status: "RUNNING", config: {...}, pnl: 0 } }
                    if (data.strategies && typeof data.strategies === 'object' && !Array.isArray(data.strategies)) {
                        data.strategies = Object.entries(data.strategies).map(([name, stratData]: [string, any]) => ({
                            name,
                            active: stratData.status === 'RUNNING',
                            status: stratData.status,
                            config: stratData.config || {},
                            pnl: stratData.pnl || 0,
                            error_count: stratData.error_count || 0
                        }));
                    }

                    setStatus(prev => ({ ...prev, ...data }));
                } catch (e) {
                    console.error("Parse error", e);
                }
            };

            ws.onclose = () => {
                setStatus(prev => ({ ...prev, backend_connected: false }));
                // Reconnect after 3 seconds
                setTimeout(connectWs, 3000);
            };

            ws.onopen = () => {
                setStatus(prev => ({ ...prev, backend_connected: true }));
            }

            return ws;
        };

        const ws = connectWs();

        return () => ws.close();
    }, []);

    const enableStrategy = async (name: string) => {
        try {
            await fetch(`/api/strategies/${name}/start`, { method: 'POST' });
        } catch (e) {
            console.error(e);
        }
    };

    const disableStrategy = async (name: string, force: boolean = false) => {
        try {
            const url = `/api/strategies/${name}/stop${force ? '?force=true' : ''}`;
            await fetch(url, { method: 'POST' });
        } catch (e) {
            console.error(e);
        }
    };

    const pauseStrategy = async (name: string) => {
        try {
            await fetch(`/api/strategies/${name}/pause`, { method: 'POST' });
        } catch (e) {
            console.error(e);
        }
    };

    const resumeStrategy = async (name: string) => {
        try {
            await fetch(`/api/strategies/${name}/resume`, { method: 'POST' });
        } catch (e) {
            console.error(e);
        }
    };

    const stopAllStrategies = async () => {
        try {
            await fetch('/api/strategies/stop_all', { method: 'POST' });
        } catch (e) {
            console.error(e);
        }
    };

    const updateStrategyConfig = async (name: string, config: any) => {
        try {
            await fetch(`/api/strategies/${name}/config`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config)
            });
        } catch (e) {
            console.error(e);
        }
    };

    return (
        <TradingContext.Provider value={{
            status,
            enableStrategy,
            disableStrategy,
            pauseStrategy,
            resumeStrategy,
            stopAllStrategies,
            updateStrategyConfig
        }}>
            {children}
        </TradingContext.Provider>
    );
};
