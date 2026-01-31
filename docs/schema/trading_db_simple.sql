-- ============================================================================
-- SIMPLIFIED TRADING DATA SCHEMA
-- Two Tables: orders (transactions) + trades (journal)
-- ============================================================================
-- Version: 1.0.0
-- Created: 2026-01-31
-- ============================================================================

-- ============================================================================
-- TABLE 1: ORDERS (Журнал заявок - заповнюється на order filled)
-- ============================================================================
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    
    -- Attribution
    strategy_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,              -- "SPXW260123P06895000.CBOE" or Combo ID
    
    -- Broker reference
    exchange_order_id TEXT UNIQUE,            -- Venue order ID (for idempotency)
    client_order_id TEXT,                     -- Our internal order ID
    
    -- Trade linkage
    trade_id TEXT,                            -- FK to trades.trade_id
    trade_type TEXT NOT NULL,                 -- CALL, PUT, CALL_CREDIT_SPREAD, etc.
    trade_direction TEXT NOT NULL,            -- ENTRY, EXIT, ADD, REDUCE
    
    -- Order details
    order_side TEXT NOT NULL,                 -- BUY, SELL
    order_type TEXT NOT NULL,                 -- MARKET, LIMIT, STOP, STOP_LIMIT
    quantity REAL NOT NULL,
    price_limit REAL,                         -- NULL for market orders
    
    -- Execution status
    status TEXT NOT NULL DEFAULT 'SUBMITTED', -- SUBMITTED, PARTIAL, FILLED, CANCELLED, REJECTED
    submitted_time TEXT NOT NULL,             -- ISO 8601 UTC
    filled_time TEXT,                         -- ISO 8601 UTC
    filled_quantity REAL DEFAULT 0,
    filled_price REAL,
    
    -- Costs
    commission REAL DEFAULT 0.0,
    
    -- Context & Audit
    raw_data TEXT,                            -- JSON with full broker response + context
    
    -- Timestamps
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    
    -- Constraints
    CONSTRAINT chk_trade_direction CHECK (trade_direction IN ('ENTRY', 'EXIT', 'ADD', 'REDUCE')),
    CONSTRAINT chk_order_side CHECK (order_side IN ('BUY', 'SELL')),
    CONSTRAINT chk_status CHECK (status IN ('SUBMITTED', 'PARTIAL', 'FILLED', 'CANCELLED', 'REJECTED'))
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_orders_strategy ON orders(strategy_id);
CREATE INDEX IF NOT EXISTS idx_orders_trade_id ON orders(trade_id);
CREATE INDEX IF NOT EXISTS idx_orders_filled_time ON orders(filled_time);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);


-- ============================================================================
-- TABLE 2: TRADES (Агрегований журнал угод)
-- ============================================================================
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    
    -- Unique identifier
    trade_id TEXT UNIQUE NOT NULL,            -- "T-SPX-20260123-001"
    
    -- Attribution
    strategy_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,               -- Primary instrument or Combo ID
    trade_type TEXT NOT NULL,                  -- CALL_CREDIT_SPREAD, PUT, etc.
    
    -- Entry context (immutable after creation)
    entry_reason TEXT,                         -- JSON: {"trigger": "BULLISH", "close": 6914.10, ...}
    entry_target_price REAL,                   -- Take profit target
    entry_stop_loss REAL,                      -- Stop loss level
    
    -- Option specifics
    strikes TEXT,                              -- JSON: ["6895P", "6890P"]
    expiration TEXT,                           -- "2026-01-23"
    legs TEXT,                                 -- JSON: [{"instrument": "...", "side": "SELL", "qty": 2, "price": 1.20}, ...]
    
    -- Strategy snapshot (for reproducibility)
    strategy_config TEXT,                      -- JSON with strategy params at entry
    
    -- Timing
    entry_time TEXT NOT NULL,                  -- ISO 8601 UTC
    exit_time TEXT,                            -- ISO 8601 UTC (NULL = still open)
    duration_seconds INTEGER,                  -- Calculated on close
    
    -- Prices
    entry_price REAL NOT NULL,                 -- Entry fill price (avg if multiple fills)
    exit_price REAL,                           -- Exit fill price (avg if multiple fills)
    
    -- Position
    quantity REAL NOT NULL,
    direction TEXT NOT NULL,                   -- LONG, SHORT
    
    -- P&L
    pnl REAL,                                  -- Realized P&L (calculated on close)
    commission REAL DEFAULT 0.0,               -- Sum of entry + exit commissions
    net_pnl REAL,                              -- pnl - commission
    result TEXT,                               -- WIN, LOSS, BREAKEVEN (calculated)
    
    -- Risk metrics
    max_profit REAL,                           -- Max possible profit
    max_loss REAL,                             -- Max possible loss (risk)
    
    -- Tracking during trade (updated periodically) - FOR STOP-LOSS TUNING
    max_unrealized_profit REAL,                -- Peak profit during trade (in $)
    max_unrealized_loss REAL,                  -- Max drawdown during trade (in $, negative)
    max_unrealized_loss_time TEXT,             -- When max drawdown occurred (ISO 8601)
    
    -- Additional drawdown analytics for SL tuning
    entry_premium_per_contract REAL,           -- Premium received per contract (for credit spreads)
    sl_distance_at_max_dd REAL,                -- How far from SL at max drawdown (in $)
    dd_recovery_time_seconds INTEGER,          -- Time from max DD to exit (if recovered)
    
    -- P&L snapshots for curve analysis (JSON array)
    pnl_snapshots TEXT,                        -- [{"ts": "...", "pnl": -50}, {"ts": "...", "pnl": 20}, ...]
    
    -- Status
    status TEXT NOT NULL DEFAULT 'OPEN',       -- OPEN, CLOSED, PARTIAL, ROLLED
    exit_reason TEXT,                          -- STOP_LOSS, TAKE_PROFIT, MANUAL, EOD, EXPIRY
    
    -- Timestamps
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    
    -- Constraints
    CONSTRAINT chk_direction CHECK (direction IN ('LONG', 'SHORT')),
    CONSTRAINT chk_trade_status CHECK (status IN ('OPEN', 'CLOSED', 'PARTIAL', 'ROLLED')),
    CONSTRAINT chk_result CHECK (result IS NULL OR result IN ('WIN', 'LOSS', 'BREAKEVEN'))
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy_id);
CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_result ON trades(result);


-- ============================================================================
-- TRIGGER: Auto-update updated_at
-- ============================================================================
CREATE TRIGGER IF NOT EXISTS trg_orders_updated_at
AFTER UPDATE ON orders
BEGIN
    UPDATE orders SET updated_at = datetime('now') WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_trades_updated_at
AFTER UPDATE ON trades
BEGIN
    UPDATE trades SET updated_at = datetime('now') WHERE id = NEW.id;
END;


-- ============================================================================
-- VIEWS: Useful aggregations
-- ============================================================================

-- Daily P&L summary
CREATE VIEW IF NOT EXISTS v_daily_summary AS
SELECT 
    DATE(exit_time) as trade_date,
    strategy_id,
    COUNT(*) as total_trades,
    SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins,
    SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) as losses,
    ROUND(100.0 * SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate,
    ROUND(SUM(pnl), 2) as gross_pnl,
    ROUND(SUM(commission), 2) as total_commission,
    ROUND(SUM(net_pnl), 2) as net_pnl,
    ROUND(AVG(net_pnl), 2) as avg_pnl,
    ROUND(MAX(net_pnl), 2) as best_trade,
    ROUND(MIN(net_pnl), 2) as worst_trade
FROM trades
WHERE status = 'CLOSED' AND exit_time IS NOT NULL
GROUP BY DATE(exit_time), strategy_id
ORDER BY trade_date DESC;

-- Open positions
CREATE VIEW IF NOT EXISTS v_open_positions AS
SELECT 
    trade_id,
    strategy_id,
    instrument_id,
    trade_type,
    direction,
    quantity,
    entry_price,
    entry_time,
    strikes,
    expiration,
    max_profit,
    max_loss,
    entry_stop_loss,
    entry_target_price
FROM trades
WHERE status = 'OPEN';

-- Strategy performance
CREATE VIEW IF NOT EXISTS v_strategy_stats AS
SELECT 
    strategy_id,
    COUNT(*) as total_trades,
    SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins,
    SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) as losses,
    ROUND(100.0 * SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate,
    ROUND(SUM(net_pnl), 2) as total_net_pnl,
    ROUND(AVG(net_pnl), 2) as avg_net_pnl,
    ROUND(MAX(net_pnl), 2) as largest_win,
    ROUND(MIN(net_pnl), 2) as largest_loss,
    ROUND(AVG(CASE WHEN result = 'WIN' THEN net_pnl END), 2) as avg_win,
    ROUND(AVG(CASE WHEN result = 'LOSS' THEN net_pnl END), 2) as avg_loss,
    ROUND(
        ABS(AVG(CASE WHEN result = 'WIN' THEN net_pnl END)) / 
        NULLIF(ABS(AVG(CASE WHEN result = 'LOSS' THEN net_pnl END)), 0), 
        2
    ) as profit_factor,
    COUNT(DISTINCT DATE(entry_time)) as trading_days
FROM trades
WHERE status = 'CLOSED'
GROUP BY strategy_id;

-- Orders for a trade
CREATE VIEW IF NOT EXISTS v_trade_orders AS
SELECT 
    t.trade_id,
    t.strategy_id,
    t.trade_type,
    t.status as trade_status,
    o.id as order_id,
    o.trade_direction,
    o.order_side,
    o.order_type,
    o.quantity,
    o.filled_quantity,
    o.filled_price,
    o.commission,
    o.status as order_status,
    o.filled_time
FROM trades t
LEFT JOIN orders o ON t.trade_id = o.trade_id
ORDER BY t.trade_id, o.filled_time;
