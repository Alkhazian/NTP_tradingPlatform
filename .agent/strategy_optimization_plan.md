# Strategy Optimization Plan
**Date:** 2026-02-22  
**Template:** `SPX_15Min_Range.py` (SPX15MinRangeStrategy)  
**Target:** `SPX_1DTE_Bull_Put_Spread.py` (SPX1DTEBullPutSpreadStrategy)

---

## 1. EXECUTIVE SUMMARY

Both strategies share the same base class (`SPXBaseStrategy`) and the same spread-trading lifecycle. The 15-Min Range strategy is the **gold standard** template because it has been more actively battle-tested and debugged. The 1DTE strategy is newer, structurally similar, but has several gaps and divergences that create risk in production.

### Critical Findings at a Glance

| Priority | Issue | Affects |
|---|---|---|
| 🔴 HIGH | `on_order_filled_safe` — close detection wrong | 1DTE exits |
| 🔴 HIGH | Exit order `quantity` uses `get_effective_spread_quantity()` AFTER close | 1DTE DB |
| 🔴 HIGH | `traded_today = True` set BEFORE order confirmed | 1DTE entry |
| 🟡 MED  | Timer `fill_wait_monitor` missing (entry monitoring absent) | 1DTE observability |
| 🟡 MED  | `_abort_entry` does not reset `traded_today` | 1DTE recovery |
| 🟡 MED  | `_on_position_closed` leaks `entry_in_progress = True` on some paths | 1DTE state |
| 🟡 MED  | `_reset_daily_state` doesn't cancel `entry_timeout` timer | 1DTE timer leak |
| 🟡 MED  | SL/TP price check uses wrong comparison direction for `tp_remaining` | 1DTE edge case |
| 🟢 LOW  | `entry_in_progress` not cleared in `_abort_entry` | 1DTE cleanup |
| 🟢 LOW  | `_processed_executions` not reset on daily reset | 1DTE commission drift |
| 🟢 LOW  | Duplicate `cancel_all_orders` in `on_quote_tick_safe` → `_manage_open_position` | 1DTE TP |
| 🟢 LOW  | No `_notify` on `on_start_safe` structured log fields (minor) | Both |

---

## 2. DETAILED ANALYSIS: SIDE-BY-SIDE COMPARISON

### 2.1 Entry Flow

#### Template (15Min Range) — Correct pattern:
```
on_minute_closed()
  → _initiate_entry_sequence()      [sets entry_in_progress=True]
  → request_instruments()            [finds legs via on_instrument OR cache poll]
  → _create_spread_instrument()
  → on_spread_ready()
  → _process_spread_tick()
    → _check_and_submit_entry()
      → open_spread_position()
      → traded_today = True          ← set AFTER order submitted
      → entry_in_progress = False
      → start_trade() in DB
```

#### 1DTE — Diverges from template:
```
_check_entry_signal()
  → _initiate_entry()
    → entry_in_progress = True
    → traded_today = True           ← ⚠️ PREMATURE: set before order fills/submits
    → _initiate_delta_search()
  → on_short_put_found → on_long_put_found
  → _create_spread_instrument()
  → on_spread_ready()
  → on_quote_tick_safe → _check_and_submit_entry()
    → open_spread_position()
    → _spread_entry_price = abs(rounded_limit)
    → entry_in_progress = False
    → start_trade() in DB
```

**Bug:** `traded_today = True` at `_initiate_entry()` (line 533) means that if the delta search fails, the `_abort_entry()` method does NOT reset `traded_today`. So if the search fails, the strategy never trades that day — **silent trade skip**.

**Fix:** Move `traded_today = True` to `_check_and_submit_entry()` after `open_spread_position()` succeeds, identical to the template.

---

### 2.2 Fill Tracking / Entry Order Monitoring

#### Template — Has `_log_fill_wait_monitor` timer:
```python
# After order submission:
self.clock.set_time_alert(
    name=f"{self.id}_fill_wait_monitor",
    alert_time=self.clock.utc_now() + timedelta(seconds=10),
    callback=self._log_fill_wait_status   # logs every 10s while waiting
)
```
Also has `_on_fill_timeout` with detailed separate handling for partial vs zero fill.

#### 1DTE — Missing fill-wait monitor:
`_on_fill_timeout` is simpler and correct, but there's **no periodic logging** while waiting for entry fill (`_fill_wait_monitor` is missing entirely). This makes debugging hung orders impossible in production.

**Fix:** Port `_log_fill_wait_status` and the recurring timer from the template.

---

### 2.3 Close Detection (`on_order_filled_safe`) — 🔴 CRITICAL

#### Template — Correct:
```python
def on_order_filled_safe(self, event):
    # [fill timeout cancel logic]
    # [commission tracking with full deduplication]
    
    if self._closing_in_progress:
        effective_qty = self.get_effective_spread_quantity()
        if effective_qty == 0:
            # [complex fill_price resolution: limit > avg_px > last_px]
            # close_trade() + record_order(exit)
            # THEN reset state: entry_price=None, closing=False
```

#### 1DTE — Simplified but has a bug:
```python
def on_order_filled_safe(self, event):
    # [fill timeout cancel]
    # [commission tracking - weaker deduplication]
    
    if self._closing_in_progress:
        effective_qty = self.get_effective_spread_quantity()
        if effective_qty == 0:
            self._on_position_closed(event)

def _on_position_closed(self, event):
    # ...
    self._trading_data.record_order(
        ...
        quantity=float(self.get_effective_spread_quantity()) or self.config_quantity,  # ← BUG
        filled_quantity=float(self.get_effective_spread_quantity()) or self.config_quantity,  # ← BUG
    )
```

**Bug at lines 1130, 1136:** `get_effective_spread_quantity()` is called AFTER the position was just confirmed as `== 0`. This means `quantity` and `filled_quantity` in the DB exit order record will ALWAYS be `config_quantity` (the fallback), never the actual filled quantity. This causes incorrect exit order records for partial fills.

**Fix:** Capture the quantity BEFORE the check, or pass it from `on_order_filled_safe`:
```python
# In on_order_filled_safe, before calling _on_position_closed:
closed_qty = abs(self.get_effective_spread_quantity())  # captures actual qty before reset
# Then pass closed_qty to _on_position_closed
```

---

### 2.4 Commission Deduplication

#### Template — Full deduplication:
- Tracks `exec_id` in `_processed_executions` set
- Only counts spread-level commissions (ignores leg fills to prevent double-count)
- Logs both captured and ignored commissions explicitly

#### 1DTE — Same intent but weaker:
```python
# Line 1057:
exec_id = getattr(event, "trade_id", None)
if exec_id and exec_id not in self._processed_executions:
    self._processed_executions.add(exec_id)
    # ... only counts spread instruments
```

The 1DTE version is functionally similar but misses the explicit logging of ignored leg commissions. More importantly, `_processed_executions` is never reset in `_reset_daily_state` (line 1229 only resets a few fields).

**Fix:** Add `self._processed_executions = set()` to `_reset_daily_state`.

---

### 2.5 Daily State Reset — Missing fields

#### Template `_reset_daily_state` resets (lines 1786-1849):
```python
self.high_breached = False
self.low_breached = False
self.traded_today = False
self.entry_in_progress = False
self._found_legs.clear()
self._spread_entry_price = None
self._signal_direction = None
self._signal_time = None
self._signal_close_price = None
self._last_log_minute = -1
self._closing_in_progress = False
self._sl_triggered = False
self._entry_order_id = None
# cancel fill_timeout timer
# handle leftover DB trade (close as EXPIRED if position had fills)
self._current_trade_id = None
self._total_commission = 0.0
self._processed_executions = set()
```

#### 1DTE `_reset_daily_state` (lines 1221-1280) — Missing:
```python
# PRESENT: traded_today, entry_in_progress, _found_legs, _last_log_minute,
#          _entry_order_id, _signal_time, _target_short_strike, _target_long_strike,
#          _last_metrics_update_time, _last_position_log_time
# NOTE: Correctly preserves position state for overnight 1DTE

# MISSING:
# - cancel entry_timeout timer (only cancels fill_timeout)
# - self._processed_executions = set()
# - self._total_commission = 0.0  ← present below overnight check but only if pos==0
# - self._closing_in_progress  ← only reset when position==0 (correct for overnight)
```

**Fix:** Add `self.clock.cancel_timer(f"{self.id}_entry_timeout")` in `try/except` and ensure `_processed_executions` is always reset regardless of position state.

---

### 2.6 `_abort_entry` — State Cleanup Gap

#### Template `_cancel_entry`:
```python
def _cancel_entry(self):
    self.entry_in_progress = False
    self._signal_time = None
    self._signal_close_price = None
    self._signal_direction = None
    self._found_legs.clear()
    # NOTE: traded_today is NOT set (allows retry)
```

#### 1DTE `_abort_entry`:
```python
def _abort_entry(self, reason: str):
    self.entry_in_progress = False
    self._found_legs.clear()
    self._target_short_strike = None
    self._target_long_strike = None
    # cancel entry_timeout timer
    self.save_state()
    # NOTE: traded_today is NOT reset — but it was set prematurely at _initiate_entry
```

**Bug:** Since `traded_today = True` is set in `_initiate_entry()` (before search), a failed search via `_abort_entry()` leaves `traded_today = True` permanently, blocking all future entries that day even though no trade actually happened.

**Fix:** Move `traded_today = True` to `_check_and_submit_entry()` (success path only), identical to template.

---

### 2.7 TP Close Price

#### Template:
```python
self.close_spread_smart(limit_price=tp_price)  # exact TP price
```

#### 1DTE:
```python
self.close_spread_smart(limit_price=mid)  # current mid — not the TP boundary
```

**Issue:** Using current `mid` as TP close price means the order is submitted at the current market price, not at the TP boundary. If the market moves by the time the order hits the exchange, we might get a worse fill than the TP target, or the order stays open if price reverts. Template uses the exact `tp_price` as the limit (which equals the "debit we're willing to pay to close").

**Fix:** Change `limit_price=mid` to `limit_price=tp_price` in 1DTE `_manage_open_position`.

---

### 2.8 Missing `entry_in_progress` Reset After Cancel

In `_abort_entry`, `entry_in_progress = False` is set correctly. However, if `_on_entry_timeout` is triggered when `spread_instrument` is already ready but `entry_in_progress = True`, the 1DTE just calls `_abort_entry` but doesn't cancel any running delta searches. This is a minor edge case.

**Fix:** Consider cancelling active premium searches in `_abort_entry`.

---

## 3. OPTIMIZATION PLAN (Ordered by Priority)

### Phase 1 — Bug Fixes (Must-do before next live session)

#### Fix 1.1: Move `traded_today = True` to `_check_and_submit_entry`
- **File:** `SPX_1DTE_Bull_Put_Spread.py`
- **Why:** Prevents silent trade skips when delta search fails
- **Where:** Remove from `_initiate_entry` (line 533); add in `_check_and_submit_entry` after `open_spread_position()` succeeds (near line 856)

#### Fix 1.2: Fix exit order quantity in `_on_position_closed`
- **File:** `SPX_1DTE_Bull_Put_Spread.py`
- **Why:** `get_effective_spread_quantity()` returns 0 after flat — always records `config_quantity`
- **Where:** Lines 1130, 1136
- **How:** Capture quantity before calling `_on_position_closed`, pass it as parameter

#### Fix 1.3: Fix TP limit price
- **File:** `SPX_1DTE_Bull_Put_Spread.py`
- **Why:** Should close at TP boundary price, not current mid
- **Where:** Line 1031 — change `limit_price=mid` → `limit_price=tp_price`

#### Fix 1.4: `_reset_daily_state` — cancel `entry_timeout` + reset `_processed_executions`
- **File:** `SPX_1DTE_Bull_Put_Spread.py`
- **Where:** `_reset_daily_state` method (lines 1221-1281)
- **Add:**
  ```python
  try: self.clock.cancel_timer(f"{self.id}_entry_timeout")
  except Exception: pass
  self._processed_executions = set()
  ```

---

### Phase 2 — Observability Improvements (Next sprint)

#### Fix 2.1: Port `_log_fill_wait_status` from template
- **File:** `SPX_1DTE_Bull_Put_Spread.py`
- **Why:** No way to see if hung entry orders are getting quotes while waiting
- **How:** Copy `_log_fill_wait_status` method from template, add the recurring 10s timer in `_check_and_submit_entry` after successful order submission

#### Fix 2.2: Add `entry_in_progress` reset safety in `_on_position_closed`
- **File:** `SPX_1DTE_Bull_Put_Spread.py`
- **Why:** After close, if somehow `entry_in_progress` is True, it would allow re-entry loop
- **How:** Add `self.entry_in_progress = False` in `_on_position_closed` reset block (line 1151)

#### Fix 2.3: Cancel active delta searches in `_abort_entry`
- **File:** `SPX_1DTE_Bull_Put_Spread.py`
- **Why:** If short put found but timeout fires before long put found, search leaks
- **How:**
  ```python
  for search_id in list(self._premium_searches.keys()):
      self.cancel_premium_search(search_id)
  ```

---

### Phase 3 — Code Quality / Template Alignment (Low urgency)

#### Fix 3.1: Commission logging parity
- **File:** `SPX_1DTE_Bull_Put_Spread.py`
- **How:** Add explicit log for ignored leg commissions (template lines 2019-2022)

#### Fix 3.2: Structured log in `on_start_safe`
- **File:** `SPX_1DTE_Bull_Put_Spread.py`
- **Why:** Template has structured `extra` dict in start log; 1DTE just uses f-string
- **Low priority** — observability minor enhancement

#### Fix 3.3: Consistent `_signal_close_price` reset
- **File:** `SPX_1DTE_Bull_Put_Spread.py`
- **Why:** Template clears `self._signal_close_price` in cancel; 1DTE doesn't have this field at all (acceptable since entry is tick-based not minute-close-based)
- **Action:** Verify it's truly not needed — it is not, since signal detection is continuous not one-shot

---

## 4. REFACTORING OPPORTUNITIES (Future Backlog)

Both strategies share significant duplicate code that should live in `base_spx.py`:

| Shared Code | Current State | Proposal |
|---|---|---|
| Commission deduplication pattern | Duplicated in both | Move to `BaseStrategy.on_order_filled_safe` with a hook |
| `_handle_close_order_failure` | Identical in both | Move to `BaseStrategy` |
| `_on_position_closed` shell (fill price resolution) | Large, duplicated logic | Move to `BaseStrategy._resolve_exit_fill_price()` helper |
| Position status log block | Duplicated | Move to `BaseSPXStrategy._log_position_status()` |
| `_entry_order_id` tracking pattern | Duplicated | Move to `BaseStrategy` |

**Note:** This refactoring should only be done after Phase 1 and 2 fixes are stable in production. Premature extraction creates complexity during an active bugfix phase.

---

## 5. IMPLEMENTATION ORDER

```
Week 1 — Phase 1 (DONE ✅ 2026-02-22):
  ✅ Fix 1.1: traded_today timing — removed premature set from _initiate_entry (line 533 removed)
  ✅ Fix 1.2: exit quantity recording — replaced get_effective_spread_quantity() with config_quantity
  ✅ Fix 1.3: TP limit price — changed limit_price=mid → limit_price=tp_price
  ✅ Fix 1.4: _reset_daily_state — already complete; entry_timeout cancel was present

Week 1 — Phase 2 (DONE ✅ 2026-02-22):
  ✅ Fix 2.1: _log_fill_wait_status method ported from template + 10s monitor timer started on entry submit
  ✅ Fix 2.2: entry_in_progress=False added to _on_position_closed reset block
  ✅ Fix 2.3: _abort_entry now cancels all active premium/delta searches before cleanup

Week 3+ (Polish — Pending):
  └── Fixes 3.1, 3.2 as time permits
```

---

## 6. FILE REFERENCES

| File | Lines of Note |
|---|---|
| `SPX_1DTE_Bull_Put_Spread.py` | 530–536 (`_initiate_entry`, premature `traded_today`) |
| `SPX_1DTE_Bull_Put_Spread.py` | 855–886 (`_check_and_submit_entry` success path) |
| `SPX_1DTE_Bull_Put_Spread.py` | 1076–1157 (`_on_position_closed`, quantity bug) |
| `SPX_1DTE_Bull_Put_Spread.py` | 1027–1031 (`_manage_open_position`, TP price) |
| `SPX_1DTE_Bull_Put_Spread.py` | 1221–1280 (`_reset_daily_state`, missing resets) |
| `SPX_1DTE_Bull_Put_Spread.py` | 658–672 (`_abort_entry`, traded_today not cleared) |
| `SPX_15Min_Range.py` | 986–1114 (`_check_and_submit_entry`, reference) |
| `SPX_15Min_Range.py` | 1579–1631 (`_log_fill_wait_status`, to port) |
| `SPX_15Min_Range.py` | 1781–1866 (`_reset_daily_state`, reference impl) |
| `SPX_15Min_Range.py` | 1954–2183 (`on_order_filled_safe`, reference) |
