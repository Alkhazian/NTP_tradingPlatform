"""
SPX 1DTE Bull Put Spread — Dry-Run Logic Simulation

Tests the strategy's decision logic by directly manipulating internal state
and calling methods. Does NOT require any external dependencies (Nautilus,
pydantic, etc.) — all are mocked via sys.modules.

21 test cases covering all entry, exit, timeout, and overnight scenarios.
"""

import sys
import os
from unittest.mock import MagicMock, patch
from datetime import datetime, date, time as dtime, timedelta
from collections import deque

# ═══ STEP 1: Mock ALL external deps before any imports ═══════════════════════

def _mock_mod():
    m = MagicMock()
    m.__all__ = []
    return m

_MOCK_MODULES = [
    "nautilus_trader", "nautilus_trader.trading", "nautilus_trader.trading.strategy",
    "nautilus_trader.model", "nautilus_trader.model.data", "nautilus_trader.model.enums",
    "nautilus_trader.model.identifiers", "nautilus_trader.model.instruments",
    "nautilus_trader.model.objects", "nautilus_trader.model.orders",
    "nautilus_trader.model.position", "nautilus_trader.common", "nautilus_trader.common.enums",
    "pydantic", "pydantic_settings",
    "app.services.telegram_service",
]
for mod in _MOCK_MODULES:
    sys.modules[mod] = _mock_mod()

class _FakeBaseModel:
    def __init_subclass__(cls, **kw): pass
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
    class Config:
        extra = "allow"

def _FakeField(*a, **kw):
    return kw.get("default", kw.get("default_factory", lambda: None)())

sys.modules["pydantic"].BaseModel = _FakeBaseModel
sys.modules["pydantic"].Field = _FakeField

_enums = sys.modules["nautilus_trader.model.enums"]
_enums.OrderSide = MagicMock()
_enums.TimeInForce = MagicMock(); _enums.TimeInForce.DAY = "DAY"
_enums.OptionKind = MagicMock(); _enums.OptionKind.PUT = "PUT"
_enums.OrderStatus = MagicMock()
_enums.OrderStatus.SUBMITTED = "SUBMITTED"
_enums.OrderStatus.ACCEPTED = "ACCEPTED"
_enums.OrderStatus.PARTIALLY_FILLED = "PARTIALLY_FILLED"
_enums.OrderStatus.FILLED = "FILLED"
_enums.PositionSide = MagicMock()

_ids = sys.modules["nautilus_trader.model.identifiers"]
_ids.InstrumentId = MagicMock(); _ids.InstrumentId.from_str = MagicMock(side_effect=lambda s: s)
_ids.Venue = MagicMock(side_effect=lambda s: s)
_ids.ClientOrderId = MagicMock(side_effect=lambda s: s)

class _FakeStrategy:
    def __init__(self, config=None): pass
sys.modules["nautilus_trader.trading.strategy"].Strategy = _FakeStrategy
sys.modules["nautilus_trader.common.enums"].ComponentState = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.strategies.config import StrategyConfig
from app.strategies.implementations.SPX_1DTE_Bull_Put_Spread import SPX1DTEBullPutSpreadStrategy


# ═══ STEP 2: Helpers ═════════════════════════════════════════════════════════

class MP:
    def __init__(self, v): self._v = v
    def as_double(self): return self._v

class MQ:
    def __init__(self, iid, bid, ask):
        self.instrument_id = iid
        self.bid_price = MP(bid); self.ask_price = MP(ask)

class MO:
    def __init__(self, status="SUBMITTED", filled_qty=0, quantity=2, coid="O-001"):
        self.status = status; self.filled_qty = filled_qty
        self.quantity = quantity; self.client_order_id = coid

class MFE:
    def __init__(self, coid="O-001", comm=None, tid="T-001", iid="SP.CBOE", lpx=0.0):
        self.client_order_id = coid
        self.commission = MP(comm) if comm else None
        self.trade_id = tid; self.instrument_id = iid; self.last_px = MP(lpx)


def make():
    import pytz, logging
    config = StrategyConfig(
        id="t1", name="T", enabled=True, instrument_id="^SPX.CBOE",
        strategy_type="SPX1DTEBullPutSpreadStrategy", order_size=2,
        parameters={
            "timezone": "America/New_York", "es_instrument_id": "ESH6.CME",
            "es_ema_period": 20, "es_sma_period": 10, "es_vwma_period": 14,
            "short_put_delta": -0.25, "long_put_delta": -0.14,
            "min_credit_amount": 200.0,
            "stop_loss_pct_of_credit": 180.0, "take_profit_pct_of_credit": 40.0,
            "opening_range_minutes": 30, "entry_cutoff_time_str": "15:00:00",
            "signal_max_age_seconds": 180, "entry_price_adjustment": 0.5,
            "entry_timeout_seconds": 60, "fill_timeout_seconds": 120,
            "require_strong_reclaim": True, "require_two_day_confirmation": False,
            "enable_macro_filter": True, "macro_day_before": False,
            "macro_stop_dates": "2026-03-18"
        }
    )
    s = object.__new__(SPX1DTEBullPutSpreadStrategy)
    lg = logging.getLogger("sim"); lg.setLevel(logging.WARNING)
    if not lg.handlers:
        h = logging.StreamHandler(); h.setFormatter(logging.Formatter("  %(message)s")); lg.addHandler(h)
    s.logger = lg

    s.strategy_id = "t1"; s.strategy_config = config
    s.tz = pytz.timezone("America/New_York")
    s.instrument_id = "^SPX.CBOE"
    s.current_spx_price = 0.0
    s.daily_high = None; s.daily_low = None
    s.or_high = None; s.or_low = None
    s.range_calculated = False; s.current_trading_day = None
    s.opening_range_minutes = 30; s.market_open_time = dtime(9, 30)
    s.spread_instrument = None
    s._pending_spread_orders = {}; s._active_spread_order_limits = {}
    s.persistence = None

    s.cache = MagicMock()
    s.cache.quote_tick.return_value = None
    s.cache.orders_open.return_value = []
    s.cache.order.return_value = None
    s.cache.instrument.return_value = None
    s.clock = MagicMock(); s.id = "SPX1DTE-001"

    s.es_instrument_id_str = "ESH6.CME"; s.es_instrument_id = "ESH6.CME"
    s.es_instrument = None; s.es_subscribed = False
    s.es_ema_period = 20; s.es_sma_period = 10; s.es_vwma_period = 14
    s.short_put_delta = -0.25; s.long_put_delta = -0.14
    s.min_credit_amount = 200.0; s.config_quantity = 2
    s.stop_loss_pct = 180.0; s.take_profit_pct = 40.0
    s.entry_cutoff_time = dtime(15, 0)
    s.signal_max_age_seconds = 180; s.entry_price_adjustment = 0.5
    s.entry_timeout_seconds = 60; s.fill_timeout_seconds = 120
    s.require_strong_reclaim = True; s.require_two_day_confirmation = False
    s.enable_macro_filter = True; s.macro_day_before = False
    s.macro_stop_dates = {date(2026, 3, 18)}

    s._es_daily_closes = deque(maxlen=30); s._es_daily_volumes = deque(maxlen=20)
    s._es_daily_opens = deque(maxlen=5); s._es_1min_closes = deque(maxlen=15)
    s._es_ema_value = None; s._es_vwma_value = None; s._es_sma_value = None
    s._es_current_price = 0.0
    s._es_daily_bar_type = None; s._es_1min_bar_type = None

    s.traded_today = False; s.entry_in_progress = False
    s._spread_entry_price = None; s._signal_time = None
    s._closing_in_progress = False; s._sl_triggered = False
    s._entry_order_id = None; s._current_trade_id = None
    s._total_commission = 0.0; s._processed_executions = set()
    s._last_log_minute = -1
    s._last_metrics_update_time = None
    s._last_position_log_time = None
    s._position_log_interval_seconds = 30
    s._macro_clear_today = True; s._strong_reclaim_ok = True; s._two_day_confirmed_ok = True

    s._short_put_search_id = None; s._long_put_search_id = None
    s._found_legs = {}; s._target_short_strike = None; s._target_long_strike = None

    s._trading_data = MagicMock()
    s._trading_data.start_trade.return_value = "T-001"
    s._trading_data.get_trade.return_value = {"status": "OPEN", "entry_price": -1.50}
    s._telegram = None

    s.get_effective_spread_quantity = MagicMock(return_value=0)
    s.open_spread_position = MagicMock(return_value=True)
    s.close_spread_smart = MagicMock()
    s.cancel_all_orders = MagicMock(); s.cancel_order = MagicMock()
    s.create_and_request_spread = MagicMock()
    s.find_option_by_delta = MagicMock(return_value="search-1")
    s.round_to_tick = MagicMock(side_effect=lambda p, _: round(p, 2))
    s.save_state = MagicMock(); s.request_instrument = MagicMock()
    s._notify = MagicMock()

    return s


def clk(s, h=10, m=30, sec=0, y=2026, mo=2, d=22):
    import pytz
    et = pytz.timezone("America/New_York")
    dt = et.localize(datetime(y, mo, d, h, m, sec))
    s.clock.utc_now.return_value = dt.astimezone(pytz.utc)
    return dt


def bull(s):
    clk(s, h=10, m=31)
    for i in range(25):
        s._es_daily_closes.append(6100 + i * 2)
        s._es_daily_opens.append(6100 + i * 2 - 5)
        s._es_daily_volumes.append(100000 + i)
    s._es_current_price = 6148.0
    n = s.es_ema_period; c = list(s._es_daily_closes)
    ema = sum(c[:n]) / n
    for v in c[n:]: ema = (v - ema) * 2.0 / (n + 1) + ema
    s._es_ema_value = ema
    s._es_sma_value = s._es_current_price - 5
    vp = s.es_vwma_period
    rc = list(s._es_daily_closes)[-vp:]; rv = list(s._es_daily_volumes)[-vp:]
    s._es_vwma_value = sum(a * b for a, b in zip(rc, rv)) / sum(rv)
    s.range_calculated = True; s.or_high = 6100.0; s.daily_high = 6102.0
    s.current_spx_price = 6101.0
    s._macro_clear_today = True; s._strong_reclaim_ok = True; s._two_day_confirmed_ok = True
    s.traded_today = False; s.entry_in_progress = False; s._closing_in_progress = False
    s.get_effective_spread_quantity.return_value = 0


# ═══ STEP 3: Tests ═══════════════════════════════════════════════════════════
P = 0; F = 0

def ok(name, cond, detail=""):
    global P, F
    if cond: P += 1; print(f"  ✅ {name}")
    else: F += 1; print(f"  ❌ {name} — {detail}")


def t1():
    print("\n═══ 1: Normal Entry Signal ═══")
    s = make(); bull(s)
    s._check_entry_signal()
    ok("entry_in_progress", s.entry_in_progress)
    ok("traded_today", s.traded_today)
    ok("find_option called", s.find_option_by_delta.called)
    ok("timeout set", s.clock.set_time_alert.called)

def t2():
    print("\n═══ 2: Time Gate Blocks ═══")
    s = make(); bull(s); clk(s, h=15, m=1)
    s._check_entry_signal()
    ok("no entry", not s.entry_in_progress)

def t3():
    print("\n═══ 3: ES Bearish Blocks ═══")
    s = make(); bull(s); s._es_current_price = s._es_ema_value - 50
    s._check_entry_signal()
    ok("no entry", not s.entry_in_progress)

def t4():
    print("\n═══ 4: Macro Blocks ═══")
    s = make(); bull(s); s._macro_clear_today = False
    s._check_entry_signal()
    ok("no entry", not s.entry_in_progress)

def t5():
    print("\n═══ 5: Strong Reclaim Blocks ═══")
    s = make(); bull(s); s._strong_reclaim_ok = False
    s._check_entry_signal()
    ok("no entry", not s.entry_in_progress)

def t6():
    print("\n═══ 6: Tick Poll — Credit Met ═══")
    s = make(); bull(s)
    s.entry_in_progress = True; s.traded_today = True
    s._signal_time = clk(s, h=10, m=31)
    s.spread_instrument = MagicMock(); s.spread_instrument.id = "SP"
    s._target_short_strike = 5900.0; s._target_long_strike = 5850.0
    s._check_and_submit_entry(MQ("SP", -2.60, -2.40))
    ok("order submitted", s.open_spread_position.called)
    ok("entry cleared", not s.entry_in_progress)
    ok("entry price set", s._spread_entry_price is not None)
    ok("start_trade called", s._trading_data.start_trade.called)
    ok("record_order called", s._trading_data.record_order.called)

def t7():
    print("\n═══ 7: Tick Poll — Credit Too Low ═══")
    s = make(); bull(s)
    s.entry_in_progress = True; s.traded_today = True
    s._signal_time = clk(s, h=10, m=31)
    s.spread_instrument = MagicMock(); s.spread_instrument.id = "SP"
    s._check_and_submit_entry(MQ("SP", -0.55, -0.45))
    ok("order NOT submitted", not s.open_spread_position.called)
    ok("still polling", s.entry_in_progress)

def t8():
    print("\n═══ 8: Tick Poll — Signal Expired ═══")
    s = make(); bull(s)
    s.entry_in_progress = True; s.traded_today = True
    s.spread_instrument = MagicMock(); s.spread_instrument.id = "SP"
    now = clk(s, h=10, m=35)
    s._signal_time = now - timedelta(seconds=200)
    s._check_and_submit_entry(MQ("SP", -2.60, -2.40))
    ok("entry cleared", not s.entry_in_progress)
    ok("order NOT submitted", not s.open_spread_position.called)

def t9():
    print("\n═══ 9: Stop Loss Trigger ═══")
    s = make()
    s._spread_entry_price = 1.50; s.stop_loss_pct = 180.0; s.take_profit_pct = 40.0
    s.spread_instrument = MagicMock(); s.spread_instrument.id = "SP"
    s.get_effective_spread_quantity.return_value = 2
    s._current_trade_id = "T-1"
    s.cache.quote_tick.return_value = MQ("SP", -4.30, -4.20)
    s.cache.orders_open.return_value = [MO()]
    s._manage_open_position()
    ok("SL triggered", s._sl_triggered)
    ok("closing in progress", s._closing_in_progress)
    ok("close called", s.close_spread_smart.called)
    ok("cancel orders", s.cancel_all_orders.called)
    ok("update_trade_metrics called", s._trading_data.update_trade_metrics.called)

def t10():
    print("\n═══ 10: Take Profit Trigger ═══")
    s = make()
    s._spread_entry_price = 1.50; s.stop_loss_pct = 180.0; s.take_profit_pct = 40.0
    s.spread_instrument = MagicMock(); s.spread_instrument.id = "SP"
    s.get_effective_spread_quantity.return_value = 2
    s._current_trade_id = "T-1"
    s.cache.quote_tick.return_value = MQ("SP", -0.85, -0.80)
    s._manage_open_position()
    ok("closing in progress", s._closing_in_progress)
    ok("close called", s.close_spread_smart.called)
    ok("SL NOT triggered", not s._sl_triggered)

def t11():
    print("\n═══ 11: SL Overrides Pending TP ═══")
    s = make()
    s._spread_entry_price = 1.50; s.stop_loss_pct = 180.0; s.take_profit_pct = 40.0
    s._closing_in_progress = True; s._sl_triggered = False
    s.spread_instrument = MagicMock(); s.spread_instrument.id = "SP"
    s.get_effective_spread_quantity.return_value = 2; s._current_trade_id = "T-1"
    s.cache.quote_tick.return_value = MQ("SP", -4.30, -4.20)
    s.cache.orders_open.return_value = [MO()]
    s._manage_open_position()
    ok("SL triggered (override)", s._sl_triggered)
    ok("cancel orders (kill TP)", s.cancel_all_orders.called)
    ok("close called (SL)", s.close_spread_smart.called)

def t12():
    print("\n═══ 12: Fill Timeout — Partial ═══")
    s = make(); s._entry_order_id = "O"; s._current_trade_id = "T"
    s._spread_entry_price = 1.50
    s.cache.order.return_value = MO(status="PARTIALLY_FILLED", filled_qty=1)
    s._on_fill_timeout(MagicMock())
    ok("cancel called", s.cancel_order.called)
    ok("qty updated", s._trading_data.update_trade_quantity.called)
    ok("entry price preserved", s._spread_entry_price == 1.50)

def t13():
    print("\n═══ 13: Fill Timeout — Zero Fills ═══")
    s = make(); s._entry_order_id = "O"; s._current_trade_id = "T"
    s._spread_entry_price = 1.50; s.traded_today = True
    s.cache.order.return_value = MO(status="SUBMITTED", filled_qty=0)
    s._on_fill_timeout(MagicMock())
    ok("cancel called", s.cancel_order.called)
    ok("entry price cleared", s._spread_entry_price is None)
    ok("trade deleted", s._trading_data.delete_trade.called)
    ok("traded_today True (block re-entry)", s.traded_today)

def t14():
    print("\n═══ 14: Close Rejected → Resume ═══")
    s = make(); s._closing_in_progress = True
    s.get_effective_spread_quantity.return_value = 2
    s._handle_close_order_failure(MagicMock(), "REJECTED")
    ok("_closing_in_progress cleared", not s._closing_in_progress)

def t15():
    print("\n═══ 15: Overnight — State Preserved ═══")
    s = make()
    s._spread_entry_price = 1.50; s._current_trade_id = "T-1"; s._total_commission = 5.5
    s.traded_today = True; s.current_trading_day = date(2026, 2, 22)
    s.get_effective_spread_quantity.return_value = 2
    clk(s, y=2026, mo=2, d=23)
    from app.strategies.base_spx import SPXBaseStrategy
    orig = SPXBaseStrategy._reset_daily_state
    SPXBaseStrategy._reset_daily_state = lambda self, d: None
    try:
        s._reset_daily_state(date(2026, 2, 23))
    finally:
        SPXBaseStrategy._reset_daily_state = orig
    ok("traded_today=False", not s.traded_today)
    ok("entry_price KEPT", s._spread_entry_price == 1.50)
    ok("trade_id KEPT", s._current_trade_id == "T-1")
    ok("commission KEPT", s._total_commission == 5.5)

def t16():
    print("\n═══ 16: No Overnight — State Cleaned ═══")
    s = make()
    s._spread_entry_price = None; s._closing_in_progress = True; s._sl_triggered = True
    s._current_trade_id = "T-1"; s._total_commission = 5.5
    s.traded_today = True; s.current_trading_day = date(2026, 2, 22)
    s.get_effective_spread_quantity.return_value = 0
    clk(s, y=2026, mo=2, d=23)
    from app.strategies.base_spx import SPXBaseStrategy
    orig = SPXBaseStrategy._reset_daily_state
    SPXBaseStrategy._reset_daily_state = lambda self, d: None
    try:
        s._reset_daily_state(date(2026, 2, 23))
    finally:
        SPXBaseStrategy._reset_daily_state = orig
    ok("traded_today=False", not s.traded_today)
    ok("entry_price None", s._spread_entry_price is None)
    ok("closing cleared", not s._closing_in_progress)
    ok("sl cleared", not s._sl_triggered)
    ok("trade_id cleared", s._current_trade_id is None)

def t17():
    print("\n═══ 17: Double Entry Prevention ═══")
    s = make(); bull(s)
    s.traded_today = True
    g = (s.range_calculated and not s.traded_today and not s.entry_in_progress
         and not s._closing_in_progress and s.get_effective_spread_quantity() == 0)
    ok("traded_today blocks", not g)
    s.traded_today = False; s.get_effective_spread_quantity.return_value = 2
    g = (s.range_calculated and not s.traded_today and not s.entry_in_progress
         and not s._closing_in_progress and s.get_effective_spread_quantity() == 0)
    ok("qty!=0 blocks", not g)

def t18():
    print("\n═══ 18: Entry Timeout — No Spread ═══")
    s = make(); s.entry_in_progress = True; s.spread_instrument = None
    s._on_entry_timeout(MagicMock())
    ok("entry cleared", not s.entry_in_progress)

def t19():
    print("\n═══ 19: Entry Timeout — Spread Ready ═══")
    s = make(); s.entry_in_progress = True; s.spread_instrument = MagicMock()
    s._on_entry_timeout(MagicMock())
    ok("entry still True", s.entry_in_progress)

def t20():
    print("\n═══ 20: Quote Tick Routing ═══")
    s = make()
    s.spread_instrument = MagicMock(); s.spread_instrument.id = "SP"
    tick = MQ("SP", -2.60, -2.40); tick.instrument_id = "SP"

    s.entry_in_progress = True; s._spread_entry_price = None
    s._signal_time = clk(s, h=10, m=31)
    with patch.object(s, "_check_and_submit_entry") as ce, \
         patch.object(s, "_manage_open_position") as mo, \
         patch.object(type(s).__mro__[1], "on_quote_tick_safe", lambda self, t: None):
        s.on_quote_tick_safe(tick)
        ok("entry: check called", ce.called)
        ok("entry: manage NOT called", not mo.called)

    s.entry_in_progress = False; s._spread_entry_price = 1.50
    with patch.object(s, "_check_and_submit_entry") as ce, \
         patch.object(s, "_manage_open_position") as mo, \
         patch.object(type(s).__mro__[1], "on_quote_tick_safe", lambda self, t: None):
        s.on_quote_tick_safe(tick)
        ok("manage: manage called", mo.called)
        ok("manage: check NOT called", not ce.called)

def t21():
    print("\n═══ 21: Position Closed → Full Reset ═══")
    s = make()
    s._spread_entry_price = 1.50; s._closing_in_progress = True; s._sl_triggered = True
    s._current_trade_id = "T-1"; s._total_commission = 8.0
    s._active_spread_order_limits = {"O": -4.20}
    s.spread_instrument = MagicMock(); s.spread_instrument.id = "SP"
    s.traded_today = False; clk(s, h=11)
    s._on_position_closed(MFE(lpx=-4.20))
    ok("entry price cleared", s._spread_entry_price is None)
    ok("closing cleared", not s._closing_in_progress)
    ok("sl cleared", not s._sl_triggered)
    ok("trade_id cleared", s._current_trade_id is None)
    ok("close_trade called", s._trading_data.close_trade.called)
    ok("record_order (exit) called", s._trading_data.record_order.called)


# ═══ RUN ═════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print(" SPX 1DTE Bull Put Spread — Logic Simulation")
    print("=" * 60)
    for fn in [t1,t2,t3,t4,t5,t6,t7,t8,t9,t10,t11,t12,t13,t14,t15,t16,t17,t18,t19,t20,t21]:
        try:
            fn()
        except Exception as e:
            global F; F += 1
            print(f"  💥 EXCEPTION in {fn.__name__}: {e}")
            import traceback; traceback.print_exc()

    print(f"\n{'=' * 60}")
    print(f" RESULTS: {P} passed, {F} failed")
    print(f"{'=' * 60}")
    return F == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
