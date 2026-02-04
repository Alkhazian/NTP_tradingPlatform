# Walkthrough: Entry Order Monitoring Implementation

## Summary
Implemented robust entry order monitoring for `SPX_15Min_Range` strategy. The system now retries entry orders with updated prices if not filled within 30 seconds (max 6 attempts).

## Files Modified

### Configuration
#### [spx_15min_range.json](file:///root/ntp-remote/data/strategies/config/spx_15min_range.json)
```diff
+        "entry_check_interval_seconds": 30,
+        "entry_max_attempts": 6
```

---

### Strategy Implementation
#### [SPX_15Min_Range.py](file:///root/ntp-remote/backend/app/strategies/implementations/SPX_15Min_Range.py)

**Key Changes:**

| Section | Change |
|---------|--------|
| Imports (L34-35) | Added `OrderStatus`, `ClientOrderId` |
| [__init__](file:///root/ntp-remote/backend/app/strategies/base.py#48-112) (L103-108) | Added 5 state variables for monitoring |
| [on_start_safe](file:///root/ntp-remote/backend/app/strategies/implementations/SPX_15Min_Range.py#121-183) (L153-155) | Load config params |
| [_check_and_submit_entry](file:///root/ntp-remote/backend/app/strategies/implementations/SPX_15Min_Range.py#801-1091) (L894-931) | Set-difference capture + start monitoring |
| New methods (L1573-1899) | 4 monitoring methods (~300 lines) |
| [_reset_daily_state](file:///root/ntp-remote/backend/app/strategies/implementations/SPX_15Min_Range.py#1535-1584) (L1559-1569) | Reset monitoring on new day |
| [get_state](file:///root/ntp-remote/backend/app/strategies/base.py#2007-2011) (L1900-1916) | Persist monitoring state |
| [set_state](file:///root/ntp-remote/backend/app/strategies/base.py#2012-2016) (L1924-1942) | Restore + resume monitoring |
| [on_order_filled_safe](file:///root/ntp-remote/backend/app/strategies/base.py#2032-2033) (L1978-2000) | Detect early fill |

---

## Logic Flow

```
Signal → Submit → Monitor (30s x 6) → {Filled? → Success : Resubmit}
                                                      ↓
                                              Max attempts? → Give up
```

## Verification
- ✅ Python syntax check passed

## Next Steps
1. Deploy to paper trading environment
2. Monitor logs for 1-2 weeks
3. Watch for patterns:
   - `🔍 Entry check` - monitoring ticks
   - `🔄 RESUBMITTING` - retry with new price
   - `✅ Entry SUCCESSFUL` - filled
   - `❌ Entry FAILED` - max attempts reached
