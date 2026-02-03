# Trading Data Field Mapping

This document describes where each field in the `trading.db` database comes from.

---

## 📊 Table: `trades`

Records complete trade lifecycle from entry to exit.

| Field | Type | Source | Description |
|-------|------|--------|-------------|
| `id` | INTEGER | Auto-generated | Primary key |
| `trade_id` | TEXT | **Generated** | `T-{strategy_id[:8]}-{YYYYMMDD-HHMMSS}` |
| `strategy_id` | TEXT | `self.strategy_id` or `str(self.id)` | Strategy instance identifier |
| `instrument_id` | TEXT | `str(order.instrument_id)` | Instrument being traded |
| `trade_type` | TEXT | Strategy-defined | `CREDIT_SPREAD`, `DAYTRADE`, `CALL`, `PUT`, etc. |
| `entry_time` | TEXT | `self.clock.utc_now().isoformat()` | ISO timestamp of entry |
| `exit_time` | TEXT | `self.clock.utc_now().isoformat()` | ISO timestamp of exit |
| `duration_seconds` | INTEGER | Calculated | `exit_time - entry_time` |
| `entry_price` | REAL | `float(event.last_px)` or spread credit | Fill price of entry order |
| `exit_price` | REAL | `float(event.last_px)` or spread debit | Fill price of exit order |
| `quantity` | INTEGER | `self.quantity` or `config.order_size` | Number of contracts |
| `direction` | TEXT | Derived from `OrderSide` | `LONG` or `SHORT` |
| `pnl` | REAL | Calculated | `(entry - exit) * qty * 100` for spreads |
| `commission` | REAL | `event.commission.as_double()` | Trading fees from IBKR |
| `net_pnl` | REAL | Calculated | `pnl - commission` |
| `result` | TEXT | Derived | `WIN` if pnl > 0, `LOSS` if pnl < 0, `EVEN` |
| `status` | TEXT | Lifecycle | `OPEN`, `CLOSED`, `CANCELLED` |
| `max_unrealized_profit` | REAL | **Tracked live** | Highest positive P&L during trade |
| `max_unrealized_loss` | REAL | **Tracked live** | Lowest negative P&L (max drawdown) |
| `strikes` | TEXT | Strategy | JSON array `["5600", "5595"]` |
| `exit_reason` | TEXT | Strategy logic | `STOP_LOSS`, `TAKE_PROFIT`, `MANUAL`, `EOD` |
| `entry_premium_per_contract` | REAL | Strategy | Credit received in cents (e.g., 150 = $1.50) |
| `pnl_snapshots` | TEXT | **Tracked live** | JSON array of `{ts, pnl}` for P&L curve |
| `config_snapshot` | TEXT | Strategy config | JSON of strategy parameters at trade time |
| `created_at` | TEXT | Auto | Database insert timestamp |
| `updated_at` | TEXT | Auto | Last update timestamp |

### Data Flow for `trades` table:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           ENTRY FLOW                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Strategy                    BaseStrategy                TradingDataService │
│  ─────────                   ────────────                ────────────────── │
│                                                                             │
│  submit_bracket_order() ──► _pending_entry_orders.add()                     │
│           │                                                                 │
│           ▼                                                                 │
│  [OrderFilled event] ────► on_order_filled()                                │
│                                   │                                         │
│                                   ▼                                         │
│                            _on_entry_filled()                               │
│                                   │                                         │
│                                   ▼                                         │
│                            _start_trade_record_async() ──► start_trade()    │
│                                                               │             │
│                                                               ▼             │
│                                                          INSERT INTO trades │
│                                                          (status='OPEN')    │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                         POSITION MANAGEMENT                                 │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Strategy                                          TradingDataService       │
│  ─────────                                         ──────────────────       │
│                                                                             │
│  _manage_open_position() ───► Calculate current P&L                         │
│           │                                                                 │
│           ▼                                                                 │
│  update_trade_metrics(pnl) ─────────────────────► update_trade_metrics()    │
│                                                           │                 │
│                                                           ▼                 │
│                                                    UPDATE trades SET        │
│                                                    max_unrealized_profit,   │
│                                                    max_unrealized_loss,     │
│                                                    pnl_snapshots            │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                            EXIT FLOW                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Strategy                    BaseStrategy                TradingDataService │
│  ─────────                   ────────────                ────────────────── │
│                                                                             │
│  [SL/TP/Manual exit]                                                        │
│           │                                                                 │
│           ▼                                                                 │
│  [OrderFilled event] ────► on_order_filled()                                │
│                                   │                                         │
│                                   ▼                                         │
│                            _on_exit_filled()                                │
│                                   │                                         │
│                                   ▼                                         │
│                            _close_trade_record_async() ──► close_trade()    │
│                                                               │             │
│                                                               ▼             │
│                                                          UPDATE trades SET  │
│                                                          exit_time,         │
│                                                          exit_price,        │
│                                                          pnl, result,       │
│                                                          status='CLOSED'    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 📋 Table: `orders`

Records individual order events (entry, exit, adjustments).

| Field | Type | Source | Description |
|-------|------|--------|-------------|
| `id` | INTEGER | Auto-generated | Primary key |
| `trade_id` | TEXT | From `trades.trade_id` | Links to parent trade |
| `strategy_id` | TEXT | `str(self.id)` | Strategy identifier |
| `instrument_id` | TEXT | `str(order.instrument_id)` | Order instrument |
| `trade_type` | TEXT | Strategy-defined | `CREDIT_SPREAD`, `CALL`, `PUT` |
| `trade_direction` | TEXT | Context | `ENTRY` or `EXIT` |
| `order_side` | TEXT | `order.side.name` | `BUY` or `SELL` |
| `order_type` | TEXT | `order.order_type.name` | `LIMIT`, `MARKET`, `STOP_LIMIT` |
| `quantity` | REAL | `float(order.quantity)` | Order quantity |
| `price_limit` | REAL | `float(order.price)` | Limit price (if applicable) |
| `price_stop` | REAL | `float(order.trigger_price)` | Stop trigger price |
| `status` | TEXT | `order.status.name` | `SUBMITTED`, `FILLED`, `CANCELED`, `REJECTED` |
| `client_order_id` | TEXT | `str(order.client_order_id)` | Nautilus order ID |
| `venue_order_id` | TEXT | `str(order.venue_order_id)` | Broker order ID |
| `submitted_time` | TEXT | `self.clock.utc_now().isoformat()` | When order was submitted |
| `filled_time` | TEXT | From `OrderFilled` event | When order was filled |
| `filled_quantity` | REAL | `float(event.last_qty)` | Actually filled quantity |
| `filled_price` | REAL | `float(event.last_px)` | Fill price |
| `commission` | REAL | `event.commission` | Order commission |
| `rejection_reason` | TEXT | From `OrderRejected` event | Why order was rejected |
| `created_at` | TEXT | Auto | Database insert timestamp |

### Data Flow for `orders` table:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         ORDER RECORDING                                     │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Strategy submits order                         TradingDataService          │
│  ─────────────────────                          ──────────────────          │
│                                                                             │
│  submit_bracket_order() ──────────────────────► record_order()              │
│       or                                              │                     │
│  submit_entry_order()                                 ▼                     │
│       or                                        INSERT INTO orders          │
│  submit_exit_order()                            (status='SUBMITTED')        │
│                                                                             │
│  [Later: OrderFilled] ────────────────────────► record_order()              │
│                                                 (with filled_* fields)      │
│                                                       │                     │
│                                                       ▼                     │
│                                                 INSERT INTO orders          │
│                                                 (status='FILLED')           │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 🔗 Field Origins by Strategy Type

### SPX_15Min_Range (Credit Spreads)

Uses **custom implementation** with `self._trading_data`:

| Field | Value Source |
|-------|--------------|
| `trade_id` | `f"T-{str(self.id)[:8]}-{date}-{time}"` |
| `entry_price` | `self._spread_entry_price` (credit received) |
| `exit_price` | `order.avg_px` (debit paid) |
| `pnl` | `(entry_credit - exit_debit) * 100` |
| `strikes` | `json.dumps([short_strike, long_strike])` |
| `entry_premium_per_contract` | `self._spread_entry_price * 100` (cents) |
| `max_unrealized_loss` | Tracked via `update_trade_metrics()` in `_manage_open_position()` |

### ORB_15_Long_Call / ORB_15_Long_Put (Single Options)

Uses **base class** trade recording:

| Field | Value Source |
|-------|--------------|
| `trade_id` | Auto-generated in `_start_trade_record_async()` |
| `entry_price` | `float(event.last_px)` |
| `exit_price` | `float(event.last_px)` |
| `pnl` | Calculated in `_close_trade_record_async()` |
| `direction` | `LONG` for BUY, `SHORT` for SELL |

---

## 🔄 Lifecycle States

### Trade Status Flow:
```
start_trade() ─────► OPEN ─────► close_trade() ─────► CLOSED
                       │
                       └──────► cancel_trade() ─────► CANCELLED
```

### Order Status Flow:
```
record_order() ─────► SUBMITTED ─────► [Fill Event] ─────► FILLED
                           │
                           └──────► [Cancel Event] ─────► CANCELED
                           │
                           └──────► [Reject Event] ─────► REJECTED
```

---

## 📈 Drawdown Tracking Detail

The `max_unrealized_loss` field is critical for stop-loss optimization.

### How it's tracked:

```python
# In strategy's position management loop (called every tick/bar):

def _manage_open_position(self):
    # Calculate current P&L
    current_pnl = (entry_price - current_price) * qty * 100
    
    # Update TradingDataService
    self._trading_data.update_trade_metrics(
        trade_id=self._current_trade_id,
        current_pnl=current_pnl  # Can be negative (loss) or positive (profit)
    )
```

### Inside TradingDataService.update_trade_metrics():

```python
def update_trade_metrics(self, trade_id: str, current_pnl: float):
    # Fetch current values
    trade = self.get_trade(trade_id)
    current_max_loss = trade['max_unrealized_loss'] or 0
    current_max_profit = trade['max_unrealized_profit'] or 0
    
    # Update extremes
    new_max_loss = min(current_max_loss, current_pnl)    # More negative = worse
    new_max_profit = max(current_max_profit, current_pnl) # More positive = better
    
    # Append to snapshots for P&L curve
    snapshots.append({"ts": now, "pnl": current_pnl})
    
    # Save to DB
    UPDATE trades SET 
        max_unrealized_loss = new_max_loss,
        max_unrealized_profit = new_max_profit,
        pnl_snapshots = json(snapshots)
    WHERE trade_id = ?
```

---

## 🎯 Key Formulas

### Credit Spread P&L:
```
entry_credit = ask_price_of_spread (positive, e.g., 1.50)
exit_debit = bid_price_of_spread (negative, e.g., -1.20)

pnl_per_spread = (entry_credit - |exit_debit|) * 100
                = (1.50 - 1.20) * 100 = $30 profit

total_pnl = pnl_per_spread * quantity
```

### Long Option P&L:
```
entry_debit = ask_price (e.g., 4.00)
exit_credit = bid_price (e.g., 4.50)

pnl_per_contract = (exit_credit - entry_debit) * 100
                 = (4.50 - 4.00) * 100 = $50 profit

total_pnl = pnl_per_contract * quantity
```

### Drawdown Percentage:
```
max_drawdown_percent = (max_unrealized_loss / entry_premium) * 100

# Example:
# Entry premium: $1.50 credit ($150)
# Max loss during trade: -$75
# Drawdown: -75 / 150 * 100 = -50%
```
