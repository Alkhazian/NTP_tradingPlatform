# Two-Layer Trading Data Storage Architecture

## Executive Summary

This document describes a two-layer storage architecture for trading data:
1. **Transactional Layer** (Orders/Ledger) - Append-only source of truth
2. **Analytical Layer** (Trades/Journal) - Derived for analytics and dashboards

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           DATA FLOW ARCHITECTURE                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────────────────────┐ │
│  │  NautilusTrader  │     │   IBKR API    │     │    Manual Trades          │ │
│  │  (Runtime)    │     │  (Reports)   │     │    (UI/External)           │ │
│  └──────┬───────┘     └──────┬───────┘     └──────────┬─────────────────┘ │
│         │                    │                        │                     │
│         ▼                    ▼                        ▼                     │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                      EVENT INGESTION LAYER                            │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                   │  │
│  │  │ OrderEvent  │  │ FillEvent   │  │ IBKRExec    │                   │  │
│  │  │ Listener    │  │ Listener    │  │ Importer    │                   │  │
│  │  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘                   │  │
│  │         │                │                │                           │  │
│  │         ▼                ▼                ▼                           │  │
│  │  ┌──────────────────────────────────────────────────────────────┐    │  │
│  │  │              Normalizer & Deduplicator                        │    │  │
│  │  │  - Assigns correlation IDs                                    │    │  │
│  │  │  - Ensures idempotency via unique constraints                │    │  │
│  │  │  - Validates data integrity                                   │    │  │
│  │  └───────────────────────────┬──────────────────────────────────┘    │  │
│  └──────────────────────────────┼───────────────────────────────────────┘  │
│                                 │                                           │
│                                 ▼                                           │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │              TRANSACTIONAL LAYER (PostgreSQL)                        │  │
│  │              Source of Truth - Append Only                           │  │
│  │  ┌─────────┐ ┌─────────────┐ ┌─────────┐ ┌───────────┐              │  │
│  │  │instruments│ │order_events │ │  fills  │ │commissions│              │  │
│  │  └─────────┘ └─────────────┘ └─────────┘ └───────────┘              │  │
│  │  ┌─────────┐ ┌─────────────┐ ┌─────────────────────────┐            │  │
│  │  │ orders  │ │strategy_runs│ │ position_snapshots     │            │  │
│  │  └─────────┘ └─────────────┘ └─────────────────────────┘            │  │
│  └──────────────────────────────┬───────────────────────────────────────┘  │
│                                 │                                           │
│         ┌───────────────────────┼───────────────────────┐                  │
│         │                       │                       │                  │
│         ▼                       ▼                       ▼                  │
│  ┌─────────────┐    ┌─────────────────┐    ┌─────────────────────────┐    │
│  │  TRADE      │    │  RECONCILIATION │    │  ANALYTICS ENGINE       │    │
│  │  BUILDER    │    │  SERVICE        │    │  (Grafana/Dashboard)    │    │
│  │             │    │                 │    │                         │    │
│  │ - Multi-leg │    │ - IBKR Match    │    │ - Real-time metrics     │    │
│  │ - Partial   │    │ - Mismatch      │    │ - P&L curves            │    │
│  │   fills     │    │   alerts        │    │ - Strategy stats        │    │
│  └──────┬──────┘    └────────┬────────┘    └─────────────────────────┘    │
│         │                    │                                             │
│         ▼                    ▼                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │              ANALYTICAL LAYER (PostgreSQL)                           │  │
│  │              Derived Data - For Dashboards                           │  │
│  │  ┌─────────┐ ┌───────────┐ ┌─────────────┐ ┌────────────┐           │  │
│  │  │ trades  │ │trade_legs │ │trade_events │ │annotations │           │  │
│  │  └─────────┘ └───────────┘ └─────────────┘ └────────────┘           │  │
│  │  ┌────────────────────┐ ┌─────────────────────────────────┐         │  │
│  │  │ reconciliation_log │ │ strategy_performance            │         │  │
│  │  └────────────────────┘ └─────────────────────────────────┘         │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Source of Truth Boundaries

| Data Field | Primary Source | Fallback Source | Notes |
|------------|---------------|-----------------|-------|
| Order creation timestamp | NautilusTrader | IBKR Reports | NT is real-time |
| Fill price | IBKR Reports | NautilusTrader | IBKR is authoritative |
| Fill quantity | IBKR Reports | NautilusTrader | IBKR is authoritative |
| Commissions | IBKR Reports | None | Only IBKR knows actual |
| Strategy attribution | NautilusTrader | Manual mapping | Via client_order_id |
| Position state | Reconstructed from fills | IBKR snapshot | Fills are truth |

---

## 3. Data Model (PostgreSQL DDL)

### 3.1 Transactional Layer

```sql
-- ============================================================================
-- INSTRUMENTS (Options with full contract details)
-- ============================================================================
CREATE TABLE instruments (
    id SERIAL PRIMARY KEY,
    instrument_id VARCHAR(100) NOT NULL UNIQUE,  -- SPXW260131C06000000.CBOE
    
    -- Core identifiers
    symbol VARCHAR(20) NOT NULL,                  -- SPX
    trading_class VARCHAR(20),                    -- SPXW
    exchange VARCHAR(20) NOT NULL,                -- CBOE
    currency VARCHAR(10) NOT NULL DEFAULT 'USD',
    
    -- Option specifics
    asset_class VARCHAR(20) NOT NULL,             -- OPTION, INDEX, SPREAD
    underlying VARCHAR(20),                       -- SPX
    expiry DATE,
    strike DECIMAL(12, 4),
    option_right VARCHAR(4),                      -- CALL, PUT
    multiplier INTEGER DEFAULT 100,
    
    -- IBKR identifiers
    con_id BIGINT,                                -- IBKR contract ID
    
    -- Metadata
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_data JSONB,                               -- Full instrument spec
    
    CONSTRAINT chk_option_fields CHECK (
        asset_class != 'OPTION' OR (
            underlying IS NOT NULL AND 
            expiry IS NOT NULL AND 
            strike IS NOT NULL AND 
            option_right IS NOT NULL
        )
    )
);

CREATE INDEX idx_instruments_symbol ON instruments(symbol);
CREATE INDEX idx_instruments_expiry ON instruments(expiry);
CREATE INDEX idx_instruments_con_id ON instruments(con_id);

-- ============================================================================
-- STRATEGY RUNS (Each strategy instance/session)
-- ============================================================================
CREATE TABLE strategy_runs (
    id SERIAL PRIMARY KEY,
    strategy_id VARCHAR(100) NOT NULL,            -- SPX_15Min_Range-001
    strategy_type VARCHAR(100) NOT NULL,          -- SPX_15Min_Range
    
    -- Run metadata
    started_at TIMESTAMPTZ NOT NULL,
    stopped_at TIMESTAMPTZ,
    
    -- Configuration snapshot (immutable for this run)
    config_snapshot JSONB NOT NULL,
    
    -- Version tracking
    code_version VARCHAR(50),                     -- git commit hash
    
    -- Status
    status VARCHAR(20) NOT NULL DEFAULT 'RUNNING',
    stop_reason TEXT,
    
    CONSTRAINT chk_status CHECK (status IN ('RUNNING', 'STOPPED', 'CRASHED', 'PAUSED'))
);

CREATE INDEX idx_strategy_runs_id ON strategy_runs(strategy_id);
CREATE INDEX idx_strategy_runs_dates ON strategy_runs(started_at, stopped_at);

-- ============================================================================
-- ORDERS (Order lifecycle tracking)
-- ============================================================================
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    
    -- Unique identifiers (for idempotency)
    client_order_id VARCHAR(100) NOT NULL UNIQUE, -- O-20260131-123456-001
    venue_order_id VARCHAR(100),                  -- IBKR assigned ID
    
    -- Attribution
    strategy_run_id INTEGER REFERENCES strategy_runs(id),
    strategy_id VARCHAR(100) NOT NULL,            -- Direct reference too
    account_id VARCHAR(50) NOT NULL,
    
    -- Instrument
    instrument_id VARCHAR(100) NOT NULL,
    
    -- Order details
    side VARCHAR(4) NOT NULL,                     -- BUY, SELL
    order_type VARCHAR(20) NOT NULL,              -- MARKET, LIMIT, etc.
    quantity DECIMAL(18, 8) NOT NULL,
    limit_price DECIMAL(18, 8),
    time_in_force VARCHAR(10) NOT NULL,           -- DAY, GTC, IOC, etc.
    
    -- Current state (derived from events, but cached for queries)
    status VARCHAR(20) NOT NULL DEFAULT 'INITIALIZED',
    filled_qty DECIMAL(18, 8) DEFAULT 0,
    avg_fill_price DECIMAL(18, 8),
    
    -- Timestamps
    ts_init TIMESTAMPTZ NOT NULL,                 -- Order created
    ts_last TIMESTAMPTZ NOT NULL,                 -- Last update
    
    -- Correlation/attribution tags
    correlation_id VARCHAR(100),                  -- Groups related orders
    parent_order_id VARCHAR(100),                 -- For OCO/bracket orders
    order_purpose VARCHAR(50),                    -- ENTRY, EXIT, STOP_LOSS, etc.
    
    -- Raw data for audit
    raw_data JSONB,
    
    CONSTRAINT chk_side CHECK (side IN ('BUY', 'SELL')),
    CONSTRAINT chk_status CHECK (status IN (
        'INITIALIZED', 'SUBMITTED', 'ACCEPTED', 'REJECTED',
        'PENDING_UPDATE', 'PENDING_CANCEL', 'CANCELED',
        'PARTIALLY_FILLED', 'FILLED', 'EXPIRED', 'TRIGGERED'
    ))
);

CREATE INDEX idx_orders_client_id ON orders(client_order_id);
CREATE INDEX idx_orders_strategy ON orders(strategy_id);
CREATE INDEX idx_orders_instrument ON orders(instrument_id);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_correlation ON orders(correlation_id);
CREATE INDEX idx_orders_ts_init ON orders(ts_init);

-- ============================================================================
-- ORDER EVENTS (Append-only audit log)
-- ============================================================================
CREATE TABLE order_events (
    id BIGSERIAL PRIMARY KEY,
    
    -- Order reference
    client_order_id VARCHAR(100) NOT NULL,
    
    -- Event details
    event_type VARCHAR(30) NOT NULL,              -- See constraint below
    event_id VARCHAR(100) NOT NULL UNIQUE,        -- For idempotency
    
    -- Event-specific data
    venue_order_id VARCHAR(100),                  -- Set on ACCEPTED
    quantity DECIMAL(18, 8),                      -- For FILLED events
    price DECIMAL(18, 8),                         -- For FILLED events
    
    -- Timestamps
    ts_event TIMESTAMPTZ NOT NULL,                -- When event occurred
    ts_ingested TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Source tracking
    source VARCHAR(20) NOT NULL,                  -- NAUTILUS, IBKR_REPORT
    raw_data JSONB,
    
    CONSTRAINT chk_event_type CHECK (event_type IN (
        'INITIALIZED', 'SUBMITTED', 'ACCEPTED', 'REJECTED',
        'PENDING_UPDATE', 'UPDATED', 'PENDING_CANCEL', 'CANCELED',
        'PARTIALLY_FILLED', 'FILLED', 'EXPIRED', 'TRIGGERED'
    ))
);

CREATE INDEX idx_order_events_order ON order_events(client_order_id);
CREATE INDEX idx_order_events_ts ON order_events(ts_event);
CREATE INDEX idx_order_events_type ON order_events(event_type);

-- ============================================================================
-- FILLS (Individual executions - supports partial fills)
-- ============================================================================
CREATE TABLE fills (
    id BIGSERIAL PRIMARY KEY,
    
    -- Unique identifier (for idempotency)
    trade_id VARCHAR(100) NOT NULL,               -- Venue trade/exec ID
    
    -- Order reference
    client_order_id VARCHAR(100) NOT NULL,
    venue_order_id VARCHAR(100),
    
    -- Attribution
    strategy_id VARCHAR(100) NOT NULL,
    account_id VARCHAR(50) NOT NULL,
    position_id VARCHAR(100),                     -- Nautilus position ID
    
    -- Instrument
    instrument_id VARCHAR(100) NOT NULL,
    
    -- Fill details
    side VARCHAR(4) NOT NULL,
    quantity DECIMAL(18, 8) NOT NULL,             -- This fill's quantity
    price DECIMAL(18, 8) NOT NULL,                -- Execution price
    
    -- Liquidity
    liquidity_side VARCHAR(10),                   -- MAKER, TAKER
    
    -- Timestamps
    ts_event TIMESTAMPTZ NOT NULL,                -- Execution time
    ts_ingested TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Source
    source VARCHAR(20) NOT NULL,                  -- NAUTILUS, IBKR_REPORT
    raw_data JSONB,
    
    CONSTRAINT uq_fills_trade UNIQUE (trade_id, source),
    CONSTRAINT chk_side CHECK (side IN ('BUY', 'SELL'))
);

CREATE INDEX idx_fills_order ON fills(client_order_id);
CREATE INDEX idx_fills_strategy ON fills(strategy_id);
CREATE INDEX idx_fills_instrument ON fills(instrument_id);
CREATE INDEX idx_fills_ts ON fills(ts_event);
CREATE INDEX idx_fills_trade_id ON fills(trade_id);

-- ============================================================================
-- COMMISSIONS (Separate table for IBKR fee breakdown)
-- ============================================================================
CREATE TABLE commissions (
    id BIGSERIAL PRIMARY KEY,
    
    -- Reference (can link to fill or order)
    fill_id BIGINT REFERENCES fills(id),
    client_order_id VARCHAR(100),
    
    -- Commission details
    amount DECIMAL(18, 8) NOT NULL,
    currency VARCHAR(10) NOT NULL DEFAULT 'USD',
    commission_type VARCHAR(30),                  -- EXECUTION, CLEARING, etc.
    
    -- Timestamps
    ts_event TIMESTAMPTZ NOT NULL,
    ts_ingested TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Source
    source VARCHAR(20) NOT NULL,                  -- NAUTILUS, IBKR_REPORT
    raw_data JSONB
);

CREATE INDEX idx_commissions_fill ON commissions(fill_id);
CREATE INDEX idx_commissions_order ON commissions(client_order_id);

-- ============================================================================
-- POSITION SNAPSHOTS (For reconstruction/audit)
-- ============================================================================
CREATE TABLE position_snapshots (
    id BIGSERIAL PRIMARY KEY,
    
    position_id VARCHAR(100) NOT NULL,
    instrument_id VARCHAR(100) NOT NULL,
    strategy_id VARCHAR(100) NOT NULL,
    account_id VARCHAR(50) NOT NULL,
    
    -- Position state
    side VARCHAR(5) NOT NULL,                     -- LONG, SHORT, FLAT
    quantity DECIMAL(18, 8) NOT NULL,
    avg_open_price DECIMAL(18, 8),
    avg_close_price DECIMAL(18, 8),
    
    -- P&L
    realized_pnl DECIMAL(18, 8),
    unrealized_pnl DECIMAL(18, 8),
    
    -- Timestamps
    ts_opened TIMESTAMPTZ,
    ts_closed TIMESTAMPTZ,
    ts_snapshot TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Snapshot reason
    snapshot_type VARCHAR(20) NOT NULL,           -- PERIODIC, CLOSE, EOD
    
    raw_data JSONB,
    
    CONSTRAINT chk_side CHECK (side IN ('LONG', 'SHORT', 'FLAT'))
);

CREATE INDEX idx_position_snapshots_pos ON position_snapshots(position_id);
CREATE INDEX idx_position_snapshots_strategy ON position_snapshots(strategy_id);
CREATE INDEX idx_position_snapshots_ts ON position_snapshots(ts_snapshot);
```

### 3.2 Analytical Layer

```sql
-- ============================================================================
-- TRADES (Logical trade entity - derived from fills)
-- ============================================================================
CREATE TABLE trades (
    id SERIAL PRIMARY KEY,
    trade_id VARCHAR(100) NOT NULL UNIQUE,        -- T-20260131-001
    
    -- Attribution
    strategy_id VARCHAR(100) NOT NULL,
    strategy_run_id INTEGER REFERENCES strategy_runs(id),
    account_id VARCHAR(50) NOT NULL,
    
    -- Trade type
    trade_type VARCHAR(30) NOT NULL,              -- SINGLE_LEG, VERTICAL, CALENDAR, etc.
    trade_direction VARCHAR(10) NOT NULL,         -- BULLISH, BEARISH, NEUTRAL
    
    -- Timing
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    duration_seconds INTEGER,
    
    -- Entry/Exit
    entry_correlation_id VARCHAR(100),            -- Groups entry orders
    exit_correlation_id VARCHAR(100),             -- Groups exit orders
    exit_reason VARCHAR(50),                      -- STOP_LOSS, TAKE_PROFIT, MANUAL, EOD
    
    -- Aggregate P&L (roll-up from legs)
    total_premium_received DECIMAL(18, 8),        -- For credit spreads
    total_premium_paid DECIMAL(18, 8),            -- For debit spreads
    realized_pnl DECIMAL(18, 8),
    total_commission DECIMAL(18, 8),
    net_pnl DECIMAL(18, 8),
    
    -- Risk metrics at entry
    max_risk DECIMAL(18, 8),                      -- Max loss possible
    max_profit DECIMAL(18, 8),                    -- Max profit possible
    
    -- Tracking
    max_drawdown DECIMAL(18, 8),
    max_profit_during DECIMAL(18, 8),
    
    -- Status
    status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
    
    CONSTRAINT chk_status CHECK (status IN ('OPEN', 'CLOSED', 'PARTIAL', 'ROLLED'))
);

CREATE INDEX idx_trades_strategy ON trades(strategy_id);
CREATE INDEX idx_trades_dates ON trades(opened_at, closed_at);
CREATE INDEX idx_trades_status ON trades(status);

-- ============================================================================
-- TRADE LEGS (Individual option legs within a trade)
-- ============================================================================
CREATE TABLE trade_legs (
    id SERIAL PRIMARY KEY,
    
    trade_id VARCHAR(100) NOT NULL REFERENCES trades(trade_id),
    leg_index INTEGER NOT NULL,                   -- 1, 2, 3, 4 for 4-leg spread
    
    -- Instrument
    instrument_id VARCHAR(100) NOT NULL,
    
    -- Option details (denormalized for queries)
    underlying VARCHAR(20),
    expiry DATE,
    strike DECIMAL(12, 4),
    option_right VARCHAR(4),
    
    -- Position
    side VARCHAR(4) NOT NULL,                     -- BUY, SELL
    quantity DECIMAL(18, 8) NOT NULL,
    
    -- Pricing
    entry_price DECIMAL(18, 8) NOT NULL,
    exit_price DECIMAL(18, 8),
    
    -- P&L for this leg
    realized_pnl DECIMAL(18, 8),
    commission DECIMAL(18, 8),
    
    -- Timing
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    
    -- Fill aggregation
    fill_count INTEGER DEFAULT 1,                 -- Number of fills aggregated
    
    UNIQUE(trade_id, leg_index)
);

CREATE INDEX idx_trade_legs_trade ON trade_legs(trade_id);
CREATE INDEX idx_trade_legs_instrument ON trade_legs(instrument_id);

-- ============================================================================
-- TRADE EVENTS (Trade lifecycle tracking)
-- ============================================================================
CREATE TABLE trade_events (
    id BIGSERIAL PRIMARY KEY,
    
    trade_id VARCHAR(100) NOT NULL REFERENCES trades(trade_id),
    event_type VARCHAR(30) NOT NULL,              -- OPENED, ADD, REDUCE, ROLLED, CLOSED
    
    ts_event TIMESTAMPTZ NOT NULL,
    
    -- Event details
    affected_legs INTEGER[],                      -- Which leg indices
    quantity_change DECIMAL(18, 8),
    price DECIMAL(18, 8),
    pnl_change DECIMAL(18, 8),
    
    notes TEXT,
    
    CONSTRAINT chk_event_type CHECK (event_type IN (
        'OPENED', 'PARTIAL_FILL', 'ADD', 'REDUCE', 'ADJUSTMENT', 'ROLLED', 'CLOSED'
    ))
);

CREATE INDEX idx_trade_events_trade ON trade_events(trade_id);
CREATE INDEX idx_trade_events_ts ON trade_events(ts_event);

-- ============================================================================
-- TRADE ANNOTATIONS (Journal/notes)
-- ============================================================================
CREATE TABLE trade_annotations (
    id SERIAL PRIMARY KEY,
    
    trade_id VARCHAR(100) NOT NULL REFERENCES trades(trade_id),
    
    -- Annotation content
    annotation_type VARCHAR(30) NOT NULL,         -- THESIS, REVIEW, LESSON, etc.
    content TEXT NOT NULL,
    
    -- Tags for filtering
    tags TEXT[],                                  -- ['breakout', 'range', 'spx']
    
    -- Market context
    market_regime VARCHAR(30),                    -- TRENDING, RANGING, VOLATILE
    
    -- Links
    screenshot_url TEXT,
    chart_url TEXT,
    
    -- Timing
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_annotations_trade ON trade_annotations(trade_id);
CREATE INDEX idx_annotations_tags ON trade_annotations USING GIN(tags);

-- ============================================================================
-- RECONCILIATION LOG (Audit trail for IBKR matching)
-- ============================================================================
CREATE TABLE reconciliation_log (
    id BIGSERIAL PRIMARY KEY,
    
    run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Scope
    date_from DATE NOT NULL,
    date_to DATE NOT NULL,
    
    -- Results
    fills_matched INTEGER DEFAULT 0,
    fills_missing_local INTEGER DEFAULT 0,        -- In IBKR but not local
    fills_missing_ibkr INTEGER DEFAULT 0,         -- In local but not IBKR
    commission_discrepancy DECIMAL(18, 8),
    
    -- Details
    mismatches JSONB,                             -- Details of each mismatch
    
    -- Resolution
    resolution_status VARCHAR(20) DEFAULT 'PENDING',
    resolution_notes TEXT,
    resolved_at TIMESTAMPTZ,
    resolved_by VARCHAR(100)
);

CREATE INDEX idx_reconciliation_dates ON reconciliation_log(date_from, date_to);
```

---

## 4. Trade Builder Algorithm

### 4.1 Core Logic (Pseudocode)

```python
class TradeBuilder:
    """
    Builds logical trades from fills in the transactional layer.
    
    Supports:
    - Single-leg options
    - Multi-leg spreads (vertical, calendar, iron condor)
    - Partial fills
    - Rolls (close + open as linked trades)
    """
    
    def __init__(self, lot_matching: str = "FIFO"):
        """
        Args:
            lot_matching: "FIFO" (recommended for options) or "LIFO"
        """
        self.lot_matching = lot_matching
        # Open positions by (instrument_id, strategy_id)
        self.open_lots: Dict[Tuple[str, str], List[Lot]] = defaultdict(list)
    
    def process_fill(self, fill: Fill) -> Optional[TradeUpdate]:
        """
        Process a single fill and update trades.
        
        Returns TradeUpdate with:
        - new_trade: If this opens a new trade
        - updated_trade: If this updates existing trade
        - closed_trade: If this closes a trade
        """
        key = (fill.instrument_id, fill.strategy_id)
        
        # Get correlation_id from the order to group related fills
        correlation_id = self._get_correlation_id(fill.client_order_id)
        
        if self._is_opening_fill(fill, correlation_id):
            return self._handle_opening_fill(fill, correlation_id)
        else:
            return self._handle_closing_fill(fill, correlation_id)
    
    def _is_opening_fill(self, fill: Fill, correlation_id: str) -> bool:
        """
        Determine if fill opens or closes position.
        
        Uses correlation_id if available, otherwise position direction.
        """
        # If order has explicit purpose (ENTRY/EXIT), use it
        order_purpose = self._get_order_purpose(fill.client_order_id)
        if order_purpose == "ENTRY":
            return True
        if order_purpose == "EXIT":
            return False
        
        # Fallback: Check if we have open position in opposite direction
        key = (fill.instrument_id, fill.strategy_id)
        open_lots = self.open_lots.get(key, [])
        
        if not open_lots:
            return True  # No position = opening
        
        # If fill direction matches open position, it's adding
        # If opposite, it's reducing/closing
        open_side = open_lots[0].side
        return fill.side == open_side
    
    def _handle_opening_fill(self, fill: Fill, correlation_id: str) -> TradeUpdate:
        """Handle fill that opens or adds to position."""
        key = (fill.instrument_id, fill.strategy_id)
        
        # Check if this is part of an existing multi-leg trade
        trade = self._find_trade_by_correlation(correlation_id)
        
        if trade is None:
            # New trade
            trade = self._create_trade(fill, correlation_id)
        
        # Create lot for FIFO/LIFO matching
        lot = Lot(
            fill_id=fill.id,
            quantity=fill.quantity,
            price=fill.price,
            side=fill.side,
            timestamp=fill.ts_event
        )
        self.open_lots[key].append(lot)
        
        # Update or create trade leg
        self._add_fill_to_trade_leg(trade, fill)
        
        return TradeUpdate(new_trade=trade if trade.is_new else None,
                          updated_trade=trade if not trade.is_new else None)
    
    def _handle_closing_fill(self, fill: Fill, correlation_id: str) -> TradeUpdate:
        """Handle fill that reduces or closes position."""
        key = (fill.instrument_id, fill.strategy_id)
        open_lots = self.open_lots.get(key, [])
        
        if not open_lots:
            # Unexpected close - log warning
            logger.warning(f"Closing fill but no open lots: {fill}")
            return None
        
        # Find the trade this fill closes
        trade = self._find_open_trade_for_instrument(fill.instrument_id, fill.strategy_id)
        if not trade:
            logger.error(f"No open trade found for closing fill: {fill}")
            return None
        
        # Apply FIFO or LIFO matching
        remaining_qty = fill.quantity
        closed_lots = []
        
        if self.lot_matching == "FIFO":
            lots_to_match = sorted(open_lots, key=lambda l: l.timestamp)
        else:  # LIFO
            lots_to_match = sorted(open_lots, key=lambda l: l.timestamp, reverse=True)
        
        for lot in lots_to_match:
            if remaining_qty <= 0:
                break
            
            match_qty = min(lot.quantity, remaining_qty)
            
            # Calculate P&L for this lot
            if fill.side == "BUY":  # Closing short
                pnl = (lot.price - fill.price) * match_qty * 100  # *100 for options
            else:  # Closing long
                pnl = (fill.price - lot.price) * match_qty * 100
            
            closed_lots.append((lot, match_qty, pnl))
            
            lot.quantity -= match_qty
            remaining_qty -= match_qty
        
        # Remove fully matched lots
        self.open_lots[key] = [l for l in open_lots if l.quantity > 0]
        
        # Update trade leg with exit details
        total_pnl = sum(pnl for _, _, pnl in closed_lots)
        self._update_trade_leg_exit(trade, fill, total_pnl)
        
        # Check if trade is fully closed
        trade_closed = self._check_trade_closed(trade)
        
        return TradeUpdate(
            updated_trade=trade if not trade_closed else None,
            closed_trade=trade if trade_closed else None
        )
    
    def _check_trade_closed(self, trade: Trade) -> bool:
        """Check if all legs of the trade are closed."""
        for leg in trade.legs:
            key = (leg.instrument_id, trade.strategy_id)
            if self.open_lots.get(key):
                return False
        
        trade.status = "CLOSED"
        trade.closed_at = datetime.utcnow()
        return True


class MultiLegTradeBuilder(TradeBuilder):
    """
    Extended trade builder that understands spread structures.
    """
    
    SPREAD_CONFIGS = {
        "VERTICAL": {"legs": 2, "same_expiry": True, "same_right": True},
        "CALENDAR": {"legs": 2, "same_strike": True, "same_right": True},
        "STRANGLE": {"legs": 2, "same_expiry": True, "different_right": True},
        "IRON_CONDOR": {"legs": 4, "same_expiry": True},
    }
    
    def detect_spread_type(self, fills: List[Fill]) -> str:
        """Detect spread type from a group of correlated fills."""
        if len(fills) == 1:
            return "SINGLE_LEG"
        
        instruments = [self._get_instrument(f.instrument_id) for f in fills]
        
        # Check for vertical spread
        if len(fills) == 2:
            if (instruments[0].expiry == instruments[1].expiry and
                instruments[0].option_right == instruments[1].option_right and
                instruments[0].strike != instruments[1].strike):
                return "VERTICAL"
            
            if (instruments[0].strike == instruments[1].strike and
                instruments[0].option_right == instruments[1].option_right and
                instruments[0].expiry != instruments[1].expiry):
                return "CALENDAR"
            
            if (instruments[0].expiry == instruments[1].expiry and
                instruments[0].option_right != instruments[1].option_right):
                return "STRANGLE"
        
        if len(fills) == 4:
            # Check for iron condor pattern
            # ... pattern matching logic
            return "IRON_CONDOR"
        
        return "CUSTOM"
    
    def aggregate_partial_fills(self, fills: List[Fill]) -> AggregatedLeg:
        """
        Aggregate multiple partial fills into a single leg entry.
        
        Calculates:
        - Total quantity
        - Volume-weighted average price
        - Total commission
        - Timestamp range
        """
        total_qty = sum(f.quantity for f in fills)
        vwap = sum(f.price * f.quantity for f in fills) / total_qty
        total_commission = sum(
            self._get_commission_for_fill(f.id) for f in fills
        )
        
        return AggregatedLeg(
            instrument_id=fills[0].instrument_id,
            side=fills[0].side,
            quantity=total_qty,
            avg_price=vwap,
            commission=total_commission,
            first_fill_at=min(f.ts_event for f in fills),
            last_fill_at=max(f.ts_event for f in fills),
            fill_count=len(fills)
        )
```

### 4.2 FIFO vs LIFO Recommendation

For **options spreads**, I recommend **FIFO (First-In-First-Out)**:

| Aspect | FIFO | LIFO |
|--------|------|------|
| Tax treatment | Matches IRS default | Requires explicit election |
| Spread integrity | Maintains original pairs | May mismatch legs |
| P&L timing | Realizes older gains first | Realizes recent gains first |
| Audit trail | Clearer lot matching | More complex tracking |

**Recommendation**: Use FIFO as default. Provide LIFO as configuration option for specific tax strategies.

### 4.3 Roll Handling

Rolls are represented as **linked trades**, not a special event type:

```python
def handle_roll(self, close_fills: List[Fill], open_fills: List[Fill]) -> Tuple[Trade, Trade]:
    """
    Handle a roll as two linked trades.
    
    This maintains clean trade accounting while preserving the roll relationship.
    """
    # Close existing trade
    closed_trade = self.process_fills(close_fills)
    closed_trade.exit_reason = "ROLLED"
    
    # Open new trade
    new_trade = self.process_fills(open_fills)
    
    # Link them
    new_trade.rolled_from = closed_trade.trade_id
    closed_trade.rolled_to = new_trade.trade_id
    
    return closed_trade, new_trade
```

---

## 5. Strategy Attribution

### 5.1 Correlation ID Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    CORRELATION ID PROPAGATION                           │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Strategy Decision                                                      │
│  ────────────────                                                       │
│  1. Strategy generates signal                                           │
│  2. Creates correlation_id: "SPX15MR-20260131-093045-BEAR"             │
│     Format: {strategy_type}-{date}-{time}-{direction}                   │
│                                                                         │
│  Order Creation                                                         │
│  ──────────────                                                         │
│  3. Set client_order_id with correlation prefix:                        │
│     "SPX15MR-20260131-093045-BEAR-O001"                                 │
│                                         └─ Order sequence               │
│                                                                         │
│  4. Store in order metadata:                                            │
│     {                                                                   │
│       "correlation_id": "SPX15MR-20260131-093045-BEAR",                │
│       "order_purpose": "ENTRY",                                         │
│       "leg_index": 1                                                    │
│     }                                                                   │
│                                                                         │
│  Fill Reception                                                         │
│  ──────────────                                                         │
│  5. Fill arrives with client_order_id                                   │
│  6. Parse correlation_id from client_order_id prefix                    │
│  7. Look up order metadata for full context                             │
│                                                                         │
│  Trade Builder                                                          │
│  ────────────                                                           │
│  8. Group fills by correlation_id                                       │
│  9. Build multi-leg trade from grouped fills                            │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Client Order ID Format

```python
# Recommended format for NautilusTrader with IBKR
CLIENT_ORDER_ID_FORMAT = "{strategy_id}-{date}-{time}-{purpose}-{seq:03d}"

# Examples:
# Entry orders:
"SPX15MR-20260131-093045-ENTRY-001"  # First leg of entry
"SPX15MR-20260131-093045-ENTRY-002"  # Second leg of entry

# Exit orders:
"SPX15MR-20260131-093045-EXIT-001"   # Exit for above entry

# Manual trades (no strategy):
"MANUAL-20260131-100000-001"
```

### 5.3 Handling Manual/External Trades

```python
def attribute_fills(fills: List[Fill]) -> Dict[str, List[Fill]]:
    """
    Attribute fills to strategies or mark as manual.
    
    Priority:
    1. Parse strategy_id from client_order_id
    2. Look up correlation in strategy_run active at fill time
    3. Check for manual trade markers
    4. Mark as UNATTRIBUTED for review
    """
    attributed = defaultdict(list)
    
    for fill in fills:
        # Try to parse from client_order_id
        strategy_id = parse_strategy_from_order_id(fill.client_order_id)
        
        if strategy_id:
            attributed[strategy_id].append(fill)
            continue
        
        # Check if venue_order_id matches known orders
        order = find_order_by_venue_id(fill.venue_order_id)
        if order:
            attributed[order.strategy_id].append(fill)
            continue
        
        # Check for IBKR FA allocation tag
        fa_profile = parse_fa_profile(fill.raw_data)
        if fa_profile:
            strategy_id = map_fa_profile_to_strategy(fa_profile)
            if strategy_id:
                attributed[strategy_id].append(fill)
                continue
        
        # Mark as unattributed for manual review
        attributed["UNATTRIBUTED"].append(fill)
    
    return attributed
```

---

## 6. IBKR Reconciliation

### 6.1 Reconciliation Process

```python
class IBKRReconciler:
    """
    Reconciles local fills with IBKR reports.
    
    Schedule: Daily at 00:15 UTC (after IBKR report generation)
    """
    
    TOLERANCE_WINDOW = timedelta(seconds=5)  # Timestamp tolerance
    PRICE_TOLERANCE = 0.0001  # Price matching tolerance
    
    async def reconcile_day(self, date: date) -> ReconciliationResult:
        """
        Reconcile all trades for a given date.
        
        Steps:
        1. Fetch IBKR flex report
        2. Load local fills
        3. Match by trade_id
        4. Handle mismatches
        5. Update commissions
        """
        # Fetch from IBKR
        ibkr_fills = await self.fetch_ibkr_executions(date)
        
        # Load local
        local_fills = await self.load_local_fills(date)
        
        # Build lookup maps
        ibkr_by_trade_id = {f.exec_id: f for f in ibkr_fills}
        local_by_trade_id = {f.trade_id: f for f in local_fills}
        
        result = ReconciliationResult(date=date)
        
        # Match fills
        for trade_id, ibkr_fill in ibkr_by_trade_id.items():
            local_fill = local_by_trade_id.get(trade_id)
            
            if local_fill is None:
                # Missing locally - need to ingest
                result.missing_local.append(ibkr_fill)
                await self.ingest_missing_fill(ibkr_fill)
            else:
                # Verify match
                discrepancy = self.compare_fills(local_fill, ibkr_fill)
                if discrepancy:
                    result.discrepancies.append(discrepancy)
                else:
                    result.matched += 1
                    # Update commission if different
                    if local_fill.commission != ibkr_fill.commission:
                        await self.update_commission(local_fill.id, ibkr_fill.commission)
        
        # Find fills we have but IBKR doesn't
        for trade_id, local_fill in local_by_trade_id.items():
            if trade_id not in ibkr_by_trade_id:
                result.missing_ibkr.append(local_fill)
        
        # Save reconciliation log
        await self.save_result(result)
        
        # Alert if significant discrepancies
        if result.missing_local or result.discrepancies:
            await self.send_alert(result)
        
        return result
    
    def compare_fills(self, local: Fill, ibkr: IBKRExecution) -> Optional[Discrepancy]:
        """Compare local fill with IBKR execution."""
        issues = []
        
        # Check quantity
        if local.quantity != ibkr.quantity:
            issues.append(f"qty: {local.quantity} vs {ibkr.quantity}")
        
        # Check price (with tolerance)
        if abs(local.price - ibkr.price) > self.PRICE_TOLERANCE:
            issues.append(f"price: {local.price} vs {ibkr.price}")
        
        # Check timestamp (with tolerance)
        time_diff = abs((local.ts_event - ibkr.exec_time).total_seconds())
        if time_diff > self.TOLERANCE_WINDOW.total_seconds():
            issues.append(f"time: {time_diff}s difference")
        
        if issues:
            return Discrepancy(
                trade_id=local.trade_id,
                local_fill=local,
                ibkr_fill=ibkr,
                issues=issues
            )
        
        return None
    
    async def handle_mismatch(self, discrepancy: Discrepancy):
        """
        Handle fill mismatch.
        
        Policy: IBKR is source of truth for prices/quantities.
        """
        # Log for audit
        logger.warning(f"Fill mismatch: {discrepancy}")
        
        # Create correction entry
        await self.create_correction_entry(discrepancy)
        
        # Alert for review
        await self.create_alert(
            type="FILL_MISMATCH",
            severity="MEDIUM",
            data=discrepancy
        )
```

### 6.2 Reconciliation Rules

| Scenario | Action | Priority |
|----------|--------|----------|
| Fill in IBKR, not local | Ingest with source=IBKR_REPORT | HIGH |
| Fill local, not in IBKR | Alert, mark as PENDING_VERIFICATION | MEDIUM |
| Price mismatch | Update local with IBKR price, create correction | MEDIUM |
| Quantity mismatch | Alert for investigation | HIGH |
| Commission differs | Update with IBKR value | LOW |
| Timestamp differs (< 5s) | Ignore | None |
| Timestamp differs (> 5s) | Log, keep IBKR value | LOW |

### 6.3 Audit Trail

Every reconciliation action creates an audit entry:

```sql
INSERT INTO audit_log (
    action_type,    -- 'FILL_CORRECTION', 'FILL_INGESTED', 'COMMISSION_UPDATE'
    entity_type,    -- 'fill', 'commission'
    entity_id,
    old_value,      -- JSONB
    new_value,      -- JSONB
    reason,
    performed_by,   -- 'RECONCILIATION_SERVICE'
    performed_at
) VALUES (...);
```

---

## 7. Repository-Specific Recommendations

### 7.1 Files to Create

```
backend/app/
├── persistence/                      # NEW - Data layer
│   ├── __init__.py
│   ├── database.py                   # PostgreSQL connection pool
│   ├── repositories/
│   │   ├── __init__.py
│   │   ├── order_repository.py       # Order CRUD
│   │   ├── fill_repository.py        # Fill CRUD
│   │   ├── trade_repository.py       # Trade CRUD
│   │   └── reconciliation_repository.py
│   ├── models/
│   │   ├── __init__.py
│   │   ├── transactional.py          # SQLAlchemy models for tx layer
│   │   └── analytical.py             # SQLAlchemy models for analytics
│   └── migrations/
│       └── versions/                  # Alembic migrations
│
├── services/
│   ├── event_listener.py             # NEW - Nautilus event hooks
│   ├── trade_builder.py              # NEW - Builds trades from fills
│   ├── ibkr_reconciler.py            # NEW - IBKR reconciliation
│   ├── drawdown_recorder.py          # REFACTOR - Use new DB
│   └── trade_recorder.py             # REFACTOR - Use new DB
│
├── strategies/
│   ├── base.py                        # MODIFY - Add event hooks
│   ├── base_spx.py                    # MODIFY - Add correlation IDs
│   └── implementations/
│       └── SPX_15Min_Range.py         # MODIFY - Generate correlation IDs
│
└── adapters/
    └── ibkr/
        ├── __init__.py
        ├── flex_report_parser.py      # NEW - Parse IBKR flex reports
        └── execution_mapper.py        # NEW - Map IBKR fields
```

### 7.2 Modification Plan

#### Step 1: Database Setup (Week 1)
```python
# backend/app/persistence/database.py
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:pass@db:5432/trading")

engine = create_async_engine(DATABASE_URL, pool_size=10)
AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)
```

#### Step 2: Event Listener Integration (Week 1-2)

Modify `BaseStrategy.on_order` and `BaseStrategy.on_order_filled`:

```python
# backend/app/strategies/base.py

class BaseStrategy(Strategy):
    
    def on_order(self, order: Order) -> None:
        """Hook into Nautilus order events."""
        super().on_order(order)
        
        # Persist order event
        asyncio.create_task(
            self._persist_order_event(order)
        )
    
    async def _persist_order_event(self, order: Order):
        """Async persist to transactional DB."""
        from app.persistence.repositories import OrderRepository
        
        event = OrderEventModel(
            client_order_id=str(order.client_order_id),
            event_type=str(order.status),
            event_id=f"{order.client_order_id}-{order.status}-{order.ts_last}",
            venue_order_id=str(order.venue_order_id) if order.venue_order_id else None,
            ts_event=pd.Timestamp(order.ts_last, unit='ns'),
            source="NAUTILUS",
            raw_data=order.to_dict()
        )
        
        await OrderRepository.save_event(event)
    
    def on_order_filled(self, order: Order, fill: OrderFilled) -> None:
        """Hook into Nautilus fill events."""
        super().on_order_filled(order, fill)
        
        # Persist fill
        asyncio.create_task(
            self._persist_fill(order, fill)
        )
    
    async def _persist_fill(self, order: Order, fill: OrderFilled):
        """Async persist fill to transactional DB."""
        from app.persistence.repositories import FillRepository
        
        fill_model = FillModel(
            trade_id=str(fill.trade_id),
            client_order_id=str(order.client_order_id),
            venue_order_id=str(order.venue_order_id),
            strategy_id=str(self.id),
            account_id=str(fill.account_id),
            instrument_id=str(fill.instrument_id),
            side=str(order.side.name),
            quantity=float(fill.last_qty),
            price=float(fill.last_px),
            liquidity_side=str(fill.liquidity_side.name) if fill.liquidity_side else None,
            ts_event=pd.Timestamp(fill.ts_event, unit='ns'),
            source="NAUTILUS",
            raw_data=fill.to_dict()
        )
        
        await FillRepository.save(fill_model)
```

#### Step 3: Correlation ID Generation (Week 2)

Modify `SPX_15Min_Range._initiate_entry_sequence()`:

```python
# backend/app/strategies/implementations/SPX_15Min_Range.py

def _initiate_entry_sequence(self):
    """Begin entry with correlation tracking."""
    # Generate correlation ID for this trade
    now = self.clock.utc_now().astimezone(self.tz)
    direction = self._signal_direction.upper()[:4]  # BEAR or BULL
    
    self._current_correlation_id = (
        f"{self.id.value}-{now.strftime('%Y%m%d-%H%M%S')}-{direction}"
    )
    
    self.logger.info(
        f"Trade correlation ID: {self._current_correlation_id}",
        extra={"extra": {"correlation_id": self._current_correlation_id}}
    )
    
    # ... rest of entry logic

def open_spread_position(self, quantity: float, is_buy: bool, limit_price: float):
    """Override to add correlation ID to order."""
    # Generate client_order_id with correlation prefix
    order_seq = self._get_next_order_seq()
    client_order_id = f"{self._current_correlation_id}-{order_seq:03d}"
    
    # Store correlation in order notes (for IBKR)
    order_metadata = {
        "correlation_id": self._current_correlation_id,
        "order_purpose": "ENTRY",
        "strategy_id": str(self.id)
    }
    
    # Call parent with custom client_order_id
    # ... order submission logic
```

#### Step 4: Trade Builder Service (Week 2-3)

```python
# backend/app/services/trade_builder.py - See full pseudocode in Section 4
```

#### Step 5: IBKR Reconciliation (Week 3-4)

```python
# backend/app/services/ibkr_reconciler.py - See full logic in Section 6
```

### 7.3 Migration Strategy (Zero Downtime)

```
Phase 1: Parallel Write (Week 1-2)
────────────────────────────────────
- Deploy new PostgreSQL schema
- Modify services to write to BOTH:
  - Existing SQLite (trade_drawdowns.db, trades.db)
  - New PostgreSQL
- No read changes yet

Phase 2: Validation (Week 3)
────────────────────────────────────
- Compare data between old and new stores
- Fix any discrepancies
- Build Trade Builder, test with historical data

Phase 3: Switch Reads (Week 4)
────────────────────────────────────
- Point dashboards to new PostgreSQL
- Keep SQLite writes as backup
- Monitor for issues

Phase 4: Cleanup (Week 5)
────────────────────────────────────
- Remove SQLite writes
- Archive old databases
- Enable IBKR reconciliation
```

---

## 8. Configuration Example

```yaml
# config/trading_data.yaml
database:
  url: postgresql+asyncpg://trading:secret@db:5432/trading
  pool_size: 10
  echo: false

ingestion:
  sources:
    - nautilus  # Real-time from NautilusTrader
    - ibkr      # Daily from IBKR Flex Reports

trade_builder:
  lot_matching: FIFO  # or LIFO
  multi_leg_detection: true
  partial_fill_aggregation: true

reconciliation:
  enabled: true
  schedule: "0 15 0 * * *"  # Daily at 00:15 UTC
  ibkr:
    flex_query_id: "12345678"
    token: "${IBKR_FLEX_TOKEN}"
  tolerance:
    timestamp_seconds: 5
    price_delta: 0.0001
  
  alerts:
    slack_webhook: "${SLACK_WEBHOOK}"
    email: "alerts@example.com"

strategies:
  correlation_id_format: "{strategy_id}-{date}-{time}-{direction}"
  
  attribution:
    fallback_strategy: "UNATTRIBUTED"
    manual_trade_prefix: "MANUAL"
```

---

## 9. Summary

### Key Design Decisions

1. **PostgreSQL over SQLite** - For concurrent access, better query capabilities, and production reliability
2. **Append-only transactional layer** - Never modify history, ensures auditability
3. **Derived analytical layer** - Rebuilt from truth, optimized for queries
4. **FIFO lot matching** - Default for options, matches tax treatment
5. **Correlation IDs in client_order_id** - Survives through IBKR round-trip
6. **IBKR as price/commission truth** - Local for real-time, IBKR for final

### Next Steps

1. Review and approve architecture
2. Set up PostgreSQL in docker-compose
3. Implement Phase 1 (parallel writes)
4. Test Trade Builder with historical data
5. Integrate IBKR Flex Report parser
6. Enable reconciliation service
