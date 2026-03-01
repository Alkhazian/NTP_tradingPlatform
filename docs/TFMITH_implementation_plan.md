# TFMITH (The First Million Is The Hardest) Implementation Plan

## 1. Overview
The **TFMITH BleedingEdge** strategy is an intraday, directional momentum breakout system. It monitors a configurable underlying symbol (default QQQ) from the market open. Once a specific time threshold is reached (default 10:10 AM EST), it checks for a minimum directional move (default ±0.25%) relative to the open. Based on the move's direction, it aggressively enters a 0 DTE Long Call or Long Put at MARKET. 
It utilizes dynamic position sizing based on a percentage of the Net Liquidation value, factoring in a persistent `loss_streak`. Trades are actively managed with profit targets, soft time stops, and hard time stops.

## 2. Configuration Parameters
These parameters will be added to the strategy configuration (e.g., in a `TFMITHConfig` class or existing `StrategyConfig`), ensuring the bot is easily configurable without modifying core logic.

- `underlying_symbol`: string (Default: 'QQQ') - Must be used across all automations instead of hardcoding QQQ.
- `position_size_0`: float - Percentage of Net Liq to allocate per trade if loss_streak = 0
- `position_size_1`: float - Percentage of Net Liq to allocate per trade if loss_streak = 1
- `position_size_2`: float - Percentage of Net Liq to allocate per trade if loss_streak = 2
- `position_size_3`: float - Percentage of Net Liq to allocate per trade if loss_streak = 3
- `position_size_4`: float - Percentage of Net Liq to allocate per trade if loss_streak = 4
- `option_delta`: float (Default: 0.45) - Target delta for the long option
- `dte`: int (Default: 0) - Days to expiration
- `entry_threshold_pct`: float (Default: 0.25) - % move required to trigger trade
- `profit_target_pct`: float (Default: 30) - % profit to close position
- `start_time`: string (Default: '10:10') - Start time for entry scanning (EST)
- `soft_end_time`: string (Default: '11:00') - Soft time-stop
- `soft_profit_target_pct`: float (Default: 5) - % profit to close position if at soft stop
- `soft_profit_flag`: bool (Default: false) - If false, close position immediately at soft_end_time. If true, check `soft_profit_target_pct`.
- `hard_end_time`: string (Default: '14:00') - Final stop time
- `start_time_str`: string (Default: '09:30:03') - The time to begin tracking the underlying, bypassing the initial seconds of market open phantom ticks.
- `allocation`: float - Base allocation of the capital for trading.

### Example Config JSON (`strategy_configs/TFMITH_QQQ.json`)
```json
{
  "strategy_id": "TFMITH_QQQ",
  "strategy_type": "TFMITH_STRATEGY",
  "is_active": true,
  "config": {
    "underlying_symbol": "QQQ",
    "position_size_0": 10.0,
    "position_size_1": 30.0,
    "position_size_2": 50.0,
    "position_size_3": 100.0,
    "position_size_4": 0.1,
    "option_delta": 0.45,
    "dte": 0,
    "entry_threshold_pct": 0.25,
    "profit_target_pct": 30.0,
    "start_time": "10:10",
    "soft_end_time": "11:00",
    "soft_profit_target_pct": 5.0,
    "soft_profit_flag": false,
    "hard_end_time": "14:00",
    "start_time_str": "09:30:03",
    "allocation": 10000.0
  }
}
```

## 3. State Management

A critical aspect of this strategy is separating daily-reset variables from persistent variables.

### Persistent States (Must survive restarts & across days)
- `loss_streak` (int, default: 0): Keeps the current streak of consecutive losses. Protected from daily resets. Resets to 0 after each winning trade. Increments on losing trades.
- `current_allocation` (float): Base allocation + realized PnL of the strategy.

> **Challenge Addressed: Tracking Current Allocation**
> `current_allocation` is initialized as the `allocation` config value. Every time a trade concludes, its absolute dollar profit or loss (PnL) is added/subtracted directly to this `current_allocation` variable. It acts as a continuous rolling bankroll that persists by calling `self.save_state()` immediately after trades finish.

### Daily States (Reset every day before market open)
- `traded_today` (bool): Prevents multiple entries in a single day or after hard_end_time.
- `opening_price` (float): Price of underlying at market open.
- `change_since_open` (float): Trailing tracked difference from the open.
- `position_open` (bool): Flag indicating an active option position.
- `entry_time` (timestamp): Time the position was opened.
- `entry_price` (float): Price of the option contract at entry.
- `trade_direction` (enum/string): 'CALL' or 'PUT'.
- `actual_position_size` (int): Number of contracts held.
- `current_option_id` (InstrumentId string): InstrumentId of the active contract.

### Example State JSON (`data/strategy_states/TFMITH_QQQ_state.json`)
```json
{
  "loss_streak": 2,
  "current_allocation": 10250.50,
  "traded_today": true,
  "opening_price": 435.20,
  "change_since_open": 0.28,
  "position_open": false,
  "entry_time": "2024-03-01T10:15:00",
  "entry_price": 2.15,
  "trade_direction": "CALL",
  "actual_position_size": 5,
  "current_option_id": "QQQ-20240301-440C"
}
```

## 4. Workflows & Automations

### Initialization
1. Subscribe to market data (1-minute) of the `underlying_symbol`.
2. Load configuration variables.
3. Restore strategy state, if exists (specifically `loss_streak` and `current_allocation`).
4. For option selection: subscribe to option chain of `underlying_symbol` (see base_spx.py for reference)
5. For option selection by delta: use similar approach as base_spx.py to find the option with the desired delta (find_option_by_delta). Use SPX_1DTE_Bull_Put. py for reference as well.

### Automation 1: Daily Reset (Runs at Market Open)
- **Trigger**: The first tick/bar received *after* `start_time_str` (e.g., 09:30:03 AM EST) of a new trading day. This delay prevents filtering phantom quotes or bad liquidity spikes right on the second of market open.
- **Logic**: Clears yesterday's state variables so the bot is ready for a new session.
- **Action**: clear state variables that require daily reset, and set the daily `opening_price`.

### Automation 2: The Scanner (Runs continuously or every 1 min on bar close)
- **Check 1**: Is Market Time >= `start_time` (10:10 AM) AND Market Time <= `soft_end_time` (11:00 AM)?
  - If No: End automation.
  - If Yes: Proceed.
- **Check 2**: Does Bot have `traded_today` TRUE?
  - If Yes: End automation (we already traded or are stopped out).
  - If No: Proceed.
- **Check 3 (Directional Bias)**: Has `underlying_symbol` moved by `entry_threshold_pct` (0.25%) since the open?
  - **Branch A (Bullish)**: If underlying price is UP >= `entry_threshold_pct` since open:
    - Action: Find long call option of `underlying_symbol` based on `option_delta`.
    - Settings: Expiration = `dte` (0 days), Strike = `option_delta` (0.45 Delta), Order_Type=MARKET.
    - Sizing Math: Calculate # of contracts: `Target Amount = current_allocation * (position_size_{loss_streak} / 100)`. `Contracts = floor(Target Amount / (Option_Price * 100))`. If `Contracts < 1`, abort trade.
    - Action: Tag bot with `traded_today=TRUE` and set `position_open=TRUE`.
    - Database: Call `trading_data_service.start_trade()` to log entry intent.
    - Action: Send long call order on `underlying_symbol` to the broker.
    - Database: On fill, call `trading_data_service.record_order()`.
    - Action: unsubscribe to unneeded market data.
  - **Branch B (Bearish)**: If underlying price is DOWN <= -`entry_threshold_pct` since open:
    - Action: Find long put option of `underlying_symbol` based on `option_delta`.
    - Settings: Expiration = `dte` (0 days), Strike = -`option_delta` (-0.45 Delta - negative delta), Order_Type=MARKET.
    - Sizing Math: Calculate # of contracts: `Target Amount = current_allocation * (position_size_{loss_streak} / 100)`. `Contracts = floor(Target Amount / (Option_Price * 100))`. If `Contracts < 1`, abort trade.
    - Action: Tag bot with `traded_today=TRUE` and set `position_open=TRUE`.
    - Database: Call `trading_data_service.start_trade()` to log entry intent.
    - Action: Send long put order on `underlying_symbol` to the broker.
    - Database: On fill, call `trading_data_service.record_order()`.
    - Action: unsubscribe to unneeded market data.

### Automation 3: The Monitor (Runs every on 1-minute bar close / or tick)
*Repeats for each open position.*
- **Check 1 (Soft Time Stop)**: Is Market Time >= `soft_end_time` (11:00 EST)?
  - If Yes: 
    - Option 1: If `soft_profit_flag`=true, run Check 4 (Soft Profit Target).
    - Option 2: If `soft_profit_flag`=false, Submit MARKET order to close.
  - If No: Proceed to Check 3 (Profit Target).
- **Check 2 (Hard Time Stop)**: Is Market Time >= `hard_end_time` (14:00 EST)?
  - If Yes: Submit MARKET order to close.
  - If No: End automation (hold position).
- **Check 3 (Profit Target)**: Is Position P/L % >= `profit_target_pct` (30%)?
  - If Yes: Submit MARKET order to close.
  - If No: End automation (hold position).
- **Check 4 (Soft Profit Target)**: Is Position P/L % >= `soft_profit_target_pct` (30% -> dynamically 5% by default)?
  - If Yes: Submit MARKET order to close.
  - If No: Proceed to Check 2 (Hard Time Stop).

### Post-Trade Check & Database Logging (`on_order_filled` logic):
When an exit order is filled:
1. **Trade DB Record**: Call `trading_data_service.record_order()` for the closing fill.
2. **Close Trade DB Record**: Call `trading_data_service.close_trade()` with final exit price, timestamp, and calculated Realized PnL.
3. **Calculate PnL**: Compute absolute PnL = (Exit Price - Entry Price) * 100 * Contracts.
4. **Update Streak & Allocation**:
   - If PnL < 0 (Position was a loss): Add `loss_streak` +1.
   - If PnL > 0 (Position was a profit): Reset `loss_streak`=0.
   - Recompute `current_allocation` = `current_allocation` + Realized PnL.
5. **State Reset & Persistence**:
   - Reset the daily active state (e.g., `position_open=False`) while maintaining `traded_today=True` to bar further trading today.
   - Call `self.save_state()` to save the new `loss_streak`, `current_allocation`, and daily states to the strategy's JSON state file.

## 5. Implementation Roadmap
1. **Configuration**: Define configuration model in `backend/app/strategies/config.py` including the multiple `position_size_{x}` parameters.
2. **Skeleton & Services**: Setup `backend/app/strategies/implementations/TFMITH_Strategy.py` inheriting **`BaseStrategy`** (from `base.py`), NOT `SPXBaseStrategy`. Initialize `TradingDataService`.
   > **Key analogues needed from SPXBaseStrategy:**
   > - **`_subscribe_to_underlying()`**: Analogous to `_subscribe_to_spx()`, handling caching and async IB requests for the configurable `underlying_symbol`. **MUST use `secType="STK"`** instead of `IND` (which SPX uses).
   > - **`on_quote_tick_safe()` / `_process_underlying_tick_unified()`**: Analogous to `_process_spx_tick_unified()`, calculating custom 1m bars from ticks and triggering `on_minute_closed()`.
   > - **`_reset_daily_state()`**: Analogous method to reset `traded_today` and tracked bounds.
   > - **`request_option_chain()`**: Method to request options via `IBContract` with 0 DTE limits. **MUST use `secType="STK"`**.
3. **Initialization**: Implement `on_start`/`on_start_safe`, capturing the `unsubscribe` logic and restoring persistent state (loss streak, allocation).
4. **Daily Reset**: Implement logic triggering exactly at `09:30 AM EST` or first tick.
5. **Scanner**: Setup the entry scanning loops evaluated on 1m bars / ticks.
6. **Entry Logic**: Implement Delta target option selection via `IBContract` and Greeks calculator using the provided recipe. Wire up `start_trade()` and `record_order()` calls to `TradingDataService`.
7. **Sizing Engine**: Map the position sizing logic accurately converting the array of `loss_streak` limits to a dynamically scaled allocation.
8. **Monitor Engine**: Wire the multi-phased state closures (Soft target, soft-close toggle, hard time stop).
9. **Exit & Cleanup**: Implement `on_order_filled` logic utilizing `close_trade()` on `TradingDataService` and persisting the final updated `loss_streak` and `current_allocation` via `self.save_state()`.
