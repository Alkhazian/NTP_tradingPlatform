-- ============================================================================
-- TRADING DATA STORAGE SCHEMA
-- Two-Layer Architecture: Transactional + Analytical
-- ============================================================================
-- Version: 1.0.0
-- Created: 2026-01-31
-- 
-- Usage:
--   psql -h localhost -U trading -d trading -f trading_db_schema.sql
-- ============================================================================

BEGIN;

-- ============================================================================
-- TRANSACTIONAL LAYER (Source of Truth - Append Only)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- INSTRUMENTS (Options with full contract details)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instruments (
    id SERIAL PRIMARY KEY,
    instrument_id VARCHAR(100) NOT NULL UNIQUE,
    
    -- Core identifiers
    symbol VARCHAR(20) NOT NULL,
    trading_class VARCHAR(20),
    exchange VARCHAR(20) NOT NULL,
    currency VARCHAR(10) NOT NULL DEFAULT 'USD',
    
    -- Asset classification
    asset_class VARCHAR(20) NOT NULL,
    underlying VARCHAR(20),
    expiry DATE,
    strike DECIMAL(12, 4),
    option_right VARCHAR(4),
    multiplier INTEGER DEFAULT 100,
    
    -- IBKR identifiers
    con_id BIGINT,
    
    -- Metadata
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_data JSONB,
    
    CONSTRAINT chk_option_fields CHECK (
        asset_class != 'OPTION' OR (
            underlying IS NOT NULL AND 
            expiry IS NOT NULL AND 
            strike IS NOT NULL AND 
            option_right IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_instruments_symbol ON instruments(symbol);
CREATE INDEX IF NOT EXISTS idx_instruments_expiry ON instruments(expiry);
CREATE INDEX IF NOT EXISTS idx_instruments_con_id ON instruments(con_id);

-- ----------------------------------------------------------------------------
-- STRATEGY RUNS (Each strategy instance/session)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_runs (
    id SERIAL PRIMARY KEY,
    strategy_id VARCHAR(100) NOT NULL,
    strategy_type VARCHAR(100) NOT NULL,
    
    -- Run metadata
    started_at TIMESTAMPTZ NOT NULL,
    stopped_at TIMESTAMPTZ,
    
    -- Configuration snapshot
    config_snapshot JSONB NOT NULL,
    code_version VARCHAR(50),
    
    -- Status
    status VARCHAR(20) NOT NULL DEFAULT 'RUNNING',
    stop_reason TEXT,
    
    CONSTRAINT chk_run_status CHECK (status IN ('RUNNING', 'STOPPED', 'CRASHED', 'PAUSED'))
);

CREATE INDEX IF NOT EXISTS idx_strategy_runs_id ON strategy_runs(strategy_id);
CREATE INDEX IF NOT EXISTS idx_strategy_runs_dates ON strategy_runs(started_at, stopped_at);

-- ----------------------------------------------------------------------------
-- ORDERS (Order lifecycle tracking)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    
    -- Unique identifiers
    client_order_id VARCHAR(100) NOT NULL UNIQUE,
    venue_order_id VARCHAR(100),
    
    -- Attribution
    strategy_run_id INTEGER REFERENCES strategy_runs(id),
    strategy_id VARCHAR(100) NOT NULL,
    account_id VARCHAR(50) NOT NULL,
    
    -- Instrument
    instrument_id VARCHAR(100) NOT NULL,
    
    -- Order details
    side VARCHAR(4) NOT NULL,
    order_type VARCHAR(20) NOT NULL,
    quantity DECIMAL(18, 8) NOT NULL,
    limit_price DECIMAL(18, 8),
    time_in_force VARCHAR(10) NOT NULL,
    
    -- Current state
    status VARCHAR(20) NOT NULL DEFAULT 'INITIALIZED',
    filled_qty DECIMAL(18, 8) DEFAULT 0,
    avg_fill_price DECIMAL(18, 8),
    
    -- Timestamps
    ts_init TIMESTAMPTZ NOT NULL,
    ts_last TIMESTAMPTZ NOT NULL,
    
    -- Correlation
    correlation_id VARCHAR(100),
    parent_order_id VARCHAR(100),
    order_purpose VARCHAR(50),
    
    raw_data JSONB,
    
    CONSTRAINT chk_order_side CHECK (side IN ('BUY', 'SELL')),
    CONSTRAINT chk_order_status CHECK (status IN (
        'INITIALIZED', 'SUBMITTED', 'ACCEPTED', 'REJECTED',
        'PENDING_UPDATE', 'PENDING_CANCEL', 'CANCELED',
        'PARTIALLY_FILLED', 'FILLED', 'EXPIRED', 'TRIGGERED'
    ))
);

CREATE INDEX IF NOT EXISTS idx_orders_client_id ON orders(client_order_id);
CREATE INDEX IF NOT EXISTS idx_orders_strategy ON orders(strategy_id);
CREATE INDEX IF NOT EXISTS idx_orders_instrument ON orders(instrument_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_correlation ON orders(correlation_id);
CREATE INDEX IF NOT EXISTS idx_orders_ts_init ON orders(ts_init);

-- ----------------------------------------------------------------------------
-- ORDER EVENTS (Append-only audit log)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_events (
    id BIGSERIAL PRIMARY KEY,
    
    client_order_id VARCHAR(100) NOT NULL,
    event_type VARCHAR(30) NOT NULL,
    event_id VARCHAR(100) NOT NULL UNIQUE,
    
    venue_order_id VARCHAR(100),
    quantity DECIMAL(18, 8),
    price DECIMAL(18, 8),
    
    ts_event TIMESTAMPTZ NOT NULL,
    ts_ingested TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    source VARCHAR(20) NOT NULL,
    raw_data JSONB,
    
    CONSTRAINT chk_event_type CHECK (event_type IN (
        'INITIALIZED', 'SUBMITTED', 'ACCEPTED', 'REJECTED',
        'PENDING_UPDATE', 'UPDATED', 'PENDING_CANCEL', 'CANCELED',
        'PARTIALLY_FILLED', 'FILLED', 'EXPIRED', 'TRIGGERED'
    ))
);

CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(client_order_id);
CREATE INDEX IF NOT EXISTS idx_order_events_ts ON order_events(ts_event);
CREATE INDEX IF NOT EXISTS idx_order_events_type ON order_events(event_type);

-- ----------------------------------------------------------------------------
-- FILLS (Individual executions - supports partial fills)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fills (
    id BIGSERIAL PRIMARY KEY,
    
    trade_id VARCHAR(100) NOT NULL,
    client_order_id VARCHAR(100) NOT NULL,
    venue_order_id VARCHAR(100),
    
    strategy_id VARCHAR(100) NOT NULL,
    account_id VARCHAR(50) NOT NULL,
    position_id VARCHAR(100),
    
    instrument_id VARCHAR(100) NOT NULL,
    
    side VARCHAR(4) NOT NULL,
    quantity DECIMAL(18, 8) NOT NULL,
    price DECIMAL(18, 8) NOT NULL,
    
    liquidity_side VARCHAR(10),
    
    ts_event TIMESTAMPTZ NOT NULL,
    ts_ingested TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    source VARCHAR(20) NOT NULL,
    raw_data JSONB,
    
    CONSTRAINT uq_fills_trade UNIQUE (trade_id, source),
    CONSTRAINT chk_fill_side CHECK (side IN ('BUY', 'SELL'))
);

CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(client_order_id);
CREATE INDEX IF NOT EXISTS idx_fills_strategy ON fills(strategy_id);
CREATE INDEX IF NOT EXISTS idx_fills_instrument ON fills(instrument_id);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts_event);
CREATE INDEX IF NOT EXISTS idx_fills_trade_id ON fills(trade_id);

-- ----------------------------------------------------------------------------
-- COMMISSIONS
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS commissions (
    id BIGSERIAL PRIMARY KEY,
    
    fill_id BIGINT REFERENCES fills(id),
    client_order_id VARCHAR(100),
    
    amount DECIMAL(18, 8) NOT NULL,
    currency VARCHAR(10) NOT NULL DEFAULT 'USD',
    commission_type VARCHAR(30),
    
    ts_event TIMESTAMPTZ NOT NULL,
    ts_ingested TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    source VARCHAR(20) NOT NULL,
    raw_data JSONB
);

CREATE INDEX IF NOT EXISTS idx_commissions_fill ON commissions(fill_id);
CREATE INDEX IF NOT EXISTS idx_commissions_order ON commissions(client_order_id);

-- ----------------------------------------------------------------------------
-- POSITION SNAPSHOTS
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS position_snapshots (
    id BIGSERIAL PRIMARY KEY,
    
    position_id VARCHAR(100) NOT NULL,
    instrument_id VARCHAR(100) NOT NULL,
    strategy_id VARCHAR(100) NOT NULL,
    account_id VARCHAR(50) NOT NULL,
    
    side VARCHAR(5) NOT NULL,
    quantity DECIMAL(18, 8) NOT NULL,
    avg_open_price DECIMAL(18, 8),
    avg_close_price DECIMAL(18, 8),
    
    realized_pnl DECIMAL(18, 8),
    unrealized_pnl DECIMAL(18, 8),
    
    ts_opened TIMESTAMPTZ,
    ts_closed TIMESTAMPTZ,
    ts_snapshot TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    snapshot_type VARCHAR(20) NOT NULL,
    
    raw_data JSONB,
    
    CONSTRAINT chk_position_side CHECK (side IN ('LONG', 'SHORT', 'FLAT'))
);

CREATE INDEX IF NOT EXISTS idx_position_snapshots_pos ON position_snapshots(position_id);
CREATE INDEX IF NOT EXISTS idx_position_snapshots_strategy ON position_snapshots(strategy_id);
CREATE INDEX IF NOT EXISTS idx_position_snapshots_ts ON position_snapshots(ts_snapshot);


-- ============================================================================
-- ANALYTICAL LAYER (Derived Data - For Dashboards)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- TRADES (Logical trade entity)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades (
    id SERIAL PRIMARY KEY,
    trade_id VARCHAR(100) NOT NULL UNIQUE,
    
    strategy_id VARCHAR(100) NOT NULL,
    strategy_run_id INTEGER REFERENCES strategy_runs(id),
    account_id VARCHAR(50) NOT NULL,
    
    trade_type VARCHAR(30) NOT NULL,
    trade_direction VARCHAR(10) NOT NULL,
    
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    duration_seconds INTEGER,
    
    entry_correlation_id VARCHAR(100),
    exit_correlation_id VARCHAR(100),
    exit_reason VARCHAR(50),
    
    total_premium_received DECIMAL(18, 8),
    total_premium_paid DECIMAL(18, 8),
    realized_pnl DECIMAL(18, 8),
    total_commission DECIMAL(18, 8),
    net_pnl DECIMAL(18, 8),
    
    max_risk DECIMAL(18, 8),
    max_profit DECIMAL(18, 8),
    
    max_drawdown DECIMAL(18, 8),
    max_profit_during DECIMAL(18, 8),
    
    rolled_from VARCHAR(100),
    rolled_to VARCHAR(100),
    
    status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
    
    CONSTRAINT chk_trade_status CHECK (status IN ('OPEN', 'CLOSED', 'PARTIAL', 'ROLLED'))
);

CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy_id);
CREATE INDEX IF NOT EXISTS idx_trades_dates ON trades(opened_at, closed_at);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);

-- ----------------------------------------------------------------------------
-- TRADE LEGS
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_legs (
    id SERIAL PRIMARY KEY,
    
    trade_id VARCHAR(100) NOT NULL REFERENCES trades(trade_id),
    leg_index INTEGER NOT NULL,
    
    instrument_id VARCHAR(100) NOT NULL,
    
    underlying VARCHAR(20),
    expiry DATE,
    strike DECIMAL(12, 4),
    option_right VARCHAR(4),
    
    side VARCHAR(4) NOT NULL,
    quantity DECIMAL(18, 8) NOT NULL,
    
    entry_price DECIMAL(18, 8) NOT NULL,
    exit_price DECIMAL(18, 8),
    
    realized_pnl DECIMAL(18, 8),
    commission DECIMAL(18, 8),
    
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    
    fill_count INTEGER DEFAULT 1,
    
    UNIQUE(trade_id, leg_index)
);

CREATE INDEX IF NOT EXISTS idx_trade_legs_trade ON trade_legs(trade_id);
CREATE INDEX IF NOT EXISTS idx_trade_legs_instrument ON trade_legs(instrument_id);

-- ----------------------------------------------------------------------------
-- TRADE EVENTS
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_events (
    id BIGSERIAL PRIMARY KEY,
    
    trade_id VARCHAR(100) NOT NULL REFERENCES trades(trade_id),
    event_type VARCHAR(30) NOT NULL,
    
    ts_event TIMESTAMPTZ NOT NULL,
    
    affected_legs INTEGER[],
    quantity_change DECIMAL(18, 8),
    price DECIMAL(18, 8),
    pnl_change DECIMAL(18, 8),
    
    notes TEXT,
    
    CONSTRAINT chk_trade_event_type CHECK (event_type IN (
        'OPENED', 'PARTIAL_FILL', 'ADD', 'REDUCE', 'ADJUSTMENT', 'ROLLED', 'CLOSED'
    ))
);

CREATE INDEX IF NOT EXISTS idx_trade_events_trade ON trade_events(trade_id);
CREATE INDEX IF NOT EXISTS idx_trade_events_ts ON trade_events(ts_event);

-- ----------------------------------------------------------------------------
-- TRADE ANNOTATIONS (Journal)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_annotations (
    id SERIAL PRIMARY KEY,
    
    trade_id VARCHAR(100) NOT NULL REFERENCES trades(trade_id),
    
    annotation_type VARCHAR(30) NOT NULL,
    content TEXT NOT NULL,
    
    tags TEXT[],
    market_regime VARCHAR(30),
    
    screenshot_url TEXT,
    chart_url TEXT,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_annotations_trade ON trade_annotations(trade_id);
CREATE INDEX IF NOT EXISTS idx_annotations_tags ON trade_annotations USING GIN(tags);

-- ----------------------------------------------------------------------------
-- RECONCILIATION LOG
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reconciliation_log (
    id BIGSERIAL PRIMARY KEY,
    
    run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    date_from DATE NOT NULL,
    date_to DATE NOT NULL,
    
    fills_matched INTEGER DEFAULT 0,
    fills_missing_local INTEGER DEFAULT 0,
    fills_missing_ibkr INTEGER DEFAULT 0,
    commission_discrepancy DECIMAL(18, 8),
    
    mismatches JSONB,
    
    resolution_status VARCHAR(20) DEFAULT 'PENDING',
    resolution_notes TEXT,
    resolved_at TIMESTAMPTZ,
    resolved_by VARCHAR(100)
);

CREATE INDEX IF NOT EXISTS idx_reconciliation_dates ON reconciliation_log(date_from, date_to);

-- ----------------------------------------------------------------------------
-- AUDIT LOG (For all corrections)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    
    action_type VARCHAR(50) NOT NULL,
    entity_type VARCHAR(50) NOT NULL,
    entity_id VARCHAR(100) NOT NULL,
    
    old_value JSONB,
    new_value JSONB,
    
    reason TEXT,
    performed_by VARCHAR(100) NOT NULL,
    performed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(performed_at);

COMMIT;

-- ============================================================================
-- HELPFUL VIEWS
-- ============================================================================

-- Daily P&L Summary
CREATE OR REPLACE VIEW v_daily_pnl AS
SELECT 
    DATE(closed_at) as trade_date,
    strategy_id,
    COUNT(*) as trade_count,
    SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
    SUM(CASE WHEN net_pnl < 0 THEN 1 ELSE 0 END) as losses,
    SUM(realized_pnl) as gross_pnl,
    SUM(total_commission) as total_commission,
    SUM(net_pnl) as net_pnl,
    AVG(net_pnl) as avg_pnl,
    MAX(net_pnl) as max_win,
    MIN(net_pnl) as max_loss,
    AVG(max_drawdown) as avg_max_drawdown
FROM trades
WHERE status = 'CLOSED'
GROUP BY DATE(closed_at), strategy_id
ORDER BY trade_date DESC;

-- Open Positions
CREATE OR REPLACE VIEW v_open_positions AS
SELECT 
    t.trade_id,
    t.strategy_id,
    t.trade_type,
    t.trade_direction,
    t.opened_at,
    t.max_risk,
    t.max_profit,
    ARRAY_AGG(DISTINCT l.strike || l.option_right) as strikes,
    SUM(l.quantity) as total_quantity
FROM trades t
JOIN trade_legs l ON t.trade_id = l.trade_id
WHERE t.status = 'OPEN'
GROUP BY t.trade_id, t.strategy_id, t.trade_type, t.trade_direction, 
         t.opened_at, t.max_risk, t.max_profit;

-- Strategy Performance
CREATE OR REPLACE VIEW v_strategy_performance AS
SELECT 
    strategy_id,
    COUNT(*) as total_trades,
    SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
    ROUND(100.0 * SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 2) as win_rate,
    SUM(net_pnl) as total_net_pnl,
    AVG(net_pnl) as avg_net_pnl,
    STDDEV(net_pnl) as pnl_stddev,
    MAX(net_pnl) as largest_win,
    MIN(net_pnl) as largest_loss,
    AVG(CASE WHEN net_pnl > 0 THEN net_pnl END) as avg_win,
    AVG(CASE WHEN net_pnl < 0 THEN net_pnl END) as avg_loss,
    CASE 
        WHEN ABS(AVG(CASE WHEN net_pnl < 0 THEN net_pnl END)) > 0 
        THEN ABS(AVG(CASE WHEN net_pnl > 0 THEN net_pnl END) / AVG(CASE WHEN net_pnl < 0 THEN net_pnl END))
        ELSE NULL 
    END as profit_factor,
    AVG(duration_seconds) / 60 as avg_duration_minutes
FROM trades
WHERE status = 'CLOSED'
GROUP BY strategy_id;

-- Fill Reconciliation Status
CREATE OR REPLACE VIEW v_fill_reconciliation AS
SELECT 
    DATE(f.ts_event) as fill_date,
    f.strategy_id,
    COUNT(*) as fill_count,
    COUNT(CASE WHEN f.source = 'NAUTILUS' THEN 1 END) as nautilus_fills,
    COUNT(CASE WHEN f.source = 'IBKR_REPORT' THEN 1 END) as ibkr_fills,
    SUM(c.amount) as total_commission
FROM fills f
LEFT JOIN commissions c ON f.id = c.fill_id
GROUP BY DATE(f.ts_event), f.strategy_id
ORDER BY fill_date DESC;
