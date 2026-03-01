"""
TFMITH Strategy — Dry-Run Logic Simulation

Tests the strategy's decision logic by directly manipulating internal state
and calling methods. Does NOT require any external dependencies (Nautilus,
pydantic, etc.) — all are mocked via sys.modules.

15 test cases covering all entry, exit, sizing, streak, and daily-reset scenarios.
"""

import sys
import os
from unittest.mock import MagicMock, patch
from datetime import datetime, date, time as dtime, timedelta

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
_enums.OrderSide.BUY = "BUY"
_enums.OrderSide.SELL = "SELL"
_enums.TimeInForce = MagicMock(); _enums.TimeInForce.DAY = "DAY"
_enums.OptionKind = MagicMock()
_enums.OptionKind.CALL = "CALL"
_enums.OptionKind.PUT = "PUT"
_enums.OrderStatus = MagicMock()
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
from app.strategies.implementations.TFMITH_Strategy import TFMITHStrategy


# ═══ STEP 2: Helpers ═════════════════════════════════════════════════════════

class MP:
    def __init__(self, v): self._v = v
    def as_double(self): return self._v
    def __float__(self): return float(self._v)

class MQ:
    def __init__(self, iid, bid, ask):
        self.instrument_id = iid
        self.bid_price = MP(bid); self.ask_price = MP(ask)

class MFE:
    """Mock Fill Event"""
    def __init__(self, coid="O-001", comm=None, iid="QQQ-OPT", lpx=0.0, lqty=5.0, side="BUY"):
        self.client_order_id = coid
        self.commission = MP(comm) if comm else None
        self.instrument_id = iid
        self.last_px = MP(lpx)
        self.last_qty = MP(lqty)
        self.order_side = MagicMock()
        self.order_side.name = side


def make():
    """Create a fresh TFMITHStrategy instance with mocked dependencies."""
    import pytz, logging
    config = StrategyConfig(
        id="tfmith_test", name="TFMITH Test", enabled=True,
        instrument_id="QQQ.NASDAQ",
        strategy_type="TFMITHStrategy", order_size=1,
        parameters={
            "timezone": "America/New_York",
            "underlying_symbol": "QQQ",
            "exchange": "SMART",
            "primary_exchange": "NASDAQ",
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
            "soft_profit_flag": False,
            "hard_end_time": "14:00",
            "start_time_str": "09:30:03",
            "allocation": 10000.0,
            "commission_per_contract": 0.65,
        }
    )
    s = object.__new__(TFMITHStrategy)
    lg = logging.getLogger("sim"); lg.setLevel(logging.WARNING)
    if not lg.handlers:
        h = logging.StreamHandler(); h.setFormatter(logging.Formatter("  %(message)s")); lg.addHandler(h)
    s.logger = lg

    s.strategy_id = "tfmith_test"; s.strategy_config = config
    s.tz = pytz.timezone("America/New_York")
    s.instrument_id = "QQQ.NASDAQ"

    # Config params
    s.underlying_symbol = "QQQ"
    s.exchange = "SMART"
    s.primary_exchange = "NASDAQ"
    s.position_size_0 = 10.0
    s.position_size_1 = 30.0
    s.position_size_2 = 50.0
    s.position_size_3 = 100.0
    s.position_size_4 = 0.1
    s.option_delta = 0.45
    s.dte = 0
    s.entry_threshold_pct = 0.25
    s.profit_target_pct = 30.0
    s.start_time = dtime(10, 10)
    s.soft_end_time = dtime(11, 0)
    s.hard_end_time = dtime(14, 0)
    s.market_open_time = dtime(9, 30, 3)
    s.soft_profit_target_pct = 5.0
    s.soft_profit_flag = False
    s.initial_allocation = 10000.0
    s.commission_per_contract = 0.65

    # Persistent
    s.loss_streak = 0
    s.current_allocation = 10000.0

    # Daily
    s.traded_today = False
    s.opening_price = None
    s.change_since_open = 0.0
    s.position_open = False
    s.entry_time = None
    s.entry_price = None
    s.trade_direction = None
    s.actual_position_size = 0
    s.current_option_id = None

    # Internal
    s.underlying_subscribed = False
    s.underlying_instrument = None
    s.underlying_instrument_id = None
    s.current_underlying_price = 0.0
    s.last_underlying_bid = 0.0
    s.last_underlying_ask = 0.0
    s.current_trading_day = None
    s._last_minute = -1
    s._option_chain_loaded = False
    s.entry_in_progress = False
    s._closing_in_progress = False
    s._current_trade_id = None
    s._entry_order_id = None
    s._exit_order_id = None
    s._actual_qty = 0.0
    s._total_commission = 0.0
    s._last_position_log_time = None
    s._position_log_interval_seconds = 30
    s._last_position_status = {}
    s._delta_searches = {}
    s._option_instrument = None
    s._option_instrument_id = None

    # Mock dependencies
    s.cache = MagicMock()
    s.cache.quote_tick.return_value = None
    s.cache.instruments.return_value = []
    s.clock = MagicMock(); s.id = "TFMITH-001"
    s.persistence = None
    s._pending_entry_orders = set()
    s._pending_exit_orders = set()

    s._trading_data = MagicMock()
    s._trading_data.start_trade.return_value = "T-001"
    s._telegram = None

    s.subscribe_quote_ticks = MagicMock()
    s.unsubscribe_quote_ticks = MagicMock()
    s.submit_order = MagicMock()
    s.order_factory = MagicMock()
    s.save_state = MagicMock()
    s._notify = MagicMock()

    return s


def clk(s, h=10, m=30, sec=0, y=2026, mo=3, d=1):
    import pytz
    et = pytz.timezone("America/New_York")
    dt = et.localize(datetime(y, mo, d, h, m, sec))
    s.clock.utc_now.return_value = dt.astimezone(pytz.utc)
    return dt


def setup_bull(s, change_pct=0.30):
    """Setup a bullish scenario."""
    s.opening_price = 500.0
    s.current_underlying_price = 500.0 * (1 + change_pct / 100)
    s.change_since_open = change_pct
    s.current_trading_day = date(2026, 3, 1)
    clk(s, h=10, m=15)


def setup_bear(s, change_pct=-0.30):
    """Setup a bearish scenario."""
    s.opening_price = 500.0
    s.current_underlying_price = 500.0 * (1 + change_pct / 100)
    s.change_since_open = change_pct
    s.current_trading_day = date(2026, 3, 1)
    clk(s, h=10, m=15)


# ═══ STEP 3: Tests ═══════════════════════════════════════════════════════════
P = 0; F = 0

def ok(name, cond, detail=""):
    global P, F
    if cond: P += 1; print(f"  ✅ {name}")
    else: F += 1; print(f"  ❌ {name} — {detail}")


def t1():
    print("\n═══ 1: Daily Reset — Persistent Vars Survive ═══")
    s = make()
    s.loss_streak = 3; s.current_allocation = 8500.0
    s.traded_today = True; s.opening_price = 500.0
    s.position_open = False; s.entry_price = 2.5
    s.current_trading_day = date(2026, 2, 28)
    clk(s, y=2026, mo=3, d=1)
    s._reset_daily_state(date(2026, 3, 1))
    ok("loss_streak preserved", s.loss_streak == 3)
    ok("allocation preserved", s.current_allocation == 8500.0)
    ok("traded_today reset", not s.traded_today)
    ok("opening_price cleared", s.opening_price is None)
    ok("entry_price cleared", s.entry_price is None)
    ok("position_open cleared", not s.position_open)


def t2():
    print("\n═══ 2: Scanner — Bullish Entry Signal ═══")
    s = make(); setup_bull(s, 0.30)
    with patch.object(s, '_initiate_entry') as ie:
        s._check_entry(close_price=s.current_underlying_price, current_time=dtime(10, 15))
        ok("_initiate_entry called", ie.called)
        ok("direction is CALL", ie.call_args[0][0] == "CALL")


def t3():
    print("\n═══ 3: Scanner — Bearish Entry Signal ═══")
    s = make(); setup_bear(s, -0.30)
    with patch.object(s, '_initiate_entry') as ie:
        s._check_entry(close_price=s.current_underlying_price, current_time=dtime(10, 15))
        ok("_initiate_entry called", ie.called)
        ok("direction is PUT", ie.call_args[0][0] == "PUT")


def t4():
    print("\n═══ 4: Scanner — Below Threshold (No Entry) ═══")
    s = make(); setup_bull(s, 0.10)  # Below 0.25%
    with patch.object(s, '_initiate_entry') as ie:
        s._check_entry(close_price=s.current_underlying_price, current_time=dtime(10, 15))
        ok("_initiate_entry NOT called", not ie.called)


def t5():
    print("\n═══ 5: Scanner — Time Gate (Before start_time) ═══")
    s = make(); setup_bull(s, 0.30)
    with patch.object(s, '_initiate_entry') as ie:
        s._check_entry(close_price=s.current_underlying_price, current_time=dtime(9, 45))
        ok("blocked before start_time", not ie.called)


def t6():
    print("\n═══ 6: Scanner — Time Gate (After soft_end_time) ═══")
    s = make(); setup_bull(s, 0.30)
    with patch.object(s, '_initiate_entry') as ie:
        s._check_entry(close_price=s.current_underlying_price, current_time=dtime(11, 5))
        ok("blocked after soft_end_time", not ie.called)


def t7():
    print("\n═══ 7: Scanner — Already Traded ═══")
    s = make(); setup_bull(s, 0.30)
    s.traded_today = True
    with patch.object(s, '_initiate_entry') as ie:
        s._check_entry(close_price=s.current_underlying_price, current_time=dtime(10, 15))
        ok("blocked by traded_today", not ie.called)


def t8():
    print("\n═══ 8: Sizing — Correct Contract Count (Streak 0) ═══")
    s = make()
    s.loss_streak = 0; s.current_allocation = 10000.0
    # 10% of 10000 = 1000. Option at $2.00 → 1000/(2*100) = 5 contracts
    contracts = s._calculate_position_size(2.0)
    ok("contracts=5", contracts == 5)


def t9():
    print("\n═══ 9: Sizing — Correct Contract Count (Streak 2) ═══")
    s = make()
    s.loss_streak = 2; s.current_allocation = 10000.0
    # 50% of 10000 = 5000. Option at $3.00 → 5000/(3*100) = 16 contracts
    contracts = s._calculate_position_size(3.0)
    ok("contracts=16", contracts == 16, f"got {contracts}")


def t10():
    print("\n═══ 10: Sizing — Zero Contracts ═══")
    s = make()
    s.loss_streak = 0; s.current_allocation = 100.0
    # 10% of 100 = 10. Option at $5.00 → 10/(5*100) = 0
    contracts = s._calculate_position_size(5.0)
    ok("contracts=0", contracts == 0)


def t11():
    print("\n═══ 11: Monitor — Profit Target Hit ═══")
    s = make()
    s.position_open = True; s.entry_price = 1.00
    s._option_instrument_id = "QQQ-OPT"
    s._option_instrument = MagicMock()
    s._option_instrument.make_qty = MagicMock(return_value=5)
    s.actual_position_size = 5; s.trade_direction = "CALL"
    # Mid = 1.35 → PnL = 35% > 30%
    s.cache.quote_tick.return_value = MQ("QQQ-OPT", 1.30, 1.40)
    with patch.object(s, '_close_position') as cp:
        s._check_monitor_exits(dtime(10, 30))
        ok("close called", cp.called)
        ok("reason=PROFIT_TARGET", cp.call_args[0][0] == "PROFIT_TARGET")


def t12():
    print("\n═══ 12: Monitor — Soft Time Stop (flag=false) ═══")
    s = make()
    s.position_open = True; s.entry_price = 1.00; s.soft_profit_flag = False
    s._option_instrument_id = "QQQ-OPT"; s.actual_position_size = 5
    # PnL = 5% (below profit target of 30%)
    s.cache.quote_tick.return_value = MQ("QQQ-OPT", 1.03, 1.07)
    with patch.object(s, '_close_position') as cp:
        s._check_monitor_exits(dtime(11, 5))  # After soft_end_time
        ok("close called", cp.called)
        ok("reason=SOFT_TIME_STOP", cp.call_args[0][0] == "SOFT_TIME_STOP")


def t13():
    print("\n═══ 13: Monitor — Soft Time Stop (flag=true) With Profit ═══")
    s = make()
    s.position_open = True; s.entry_price = 1.00; s.soft_profit_flag = True
    s._option_instrument_id = "QQQ-OPT"; s.actual_position_size = 5
    # Mid = 1.06 → PnL = 6% > soft_profit_target (5%)
    s.cache.quote_tick.return_value = MQ("QQQ-OPT", 1.04, 1.08)
    with patch.object(s, '_close_position') as cp:
        s._check_monitor_exits(dtime(11, 5))  # After soft time
        ok("close called", cp.called)
        ok("reason=SOFT_PROFIT_TARGET", cp.call_args[0][0] == "SOFT_PROFIT_TARGET")


def t14():
    print("\n═══ 14: Monitor — Soft Time Stop (flag=true) No Profit → Hard Stop ═══")
    s = make()
    s.position_open = True; s.entry_price = 1.00; s.soft_profit_flag = True
    s._option_instrument_id = "QQQ-OPT"; s.actual_position_size = 5
    # Mid = 1.02 → PnL = 2% < soft_profit_target (5%), check hard time
    s.cache.quote_tick.return_value = MQ("QQQ-OPT", 1.01, 1.03)
    with patch.object(s, '_close_position') as cp:
        # After soft but before hard → no close
        s._check_monitor_exits(dtime(11, 30))
        ok("no close before hard stop", not cp.called)
    with patch.object(s, '_close_position') as cp:
        # At hard_end_time
        s._check_monitor_exits(dtime(14, 0))
        ok("close at hard stop", cp.called)
        ok("reason=HARD_TIME_STOP", cp.call_args[0][0] == "HARD_TIME_STOP")


def t15():
    print("\n═══ 15: Monitor — Hard Time Stop ═══")
    s = make()
    s.position_open = True; s.entry_price = 1.00
    s._option_instrument_id = "QQQ-OPT"; s.actual_position_size = 5
    # Set soft_profit_flag=True so soft stop doesn't close immediately
    # and PnL is below soft_profit_target so it falls through to hard
    s.soft_profit_flag = True
    # PnL = -10%, some profit threshold not met
    s.cache.quote_tick.return_value = MQ("QQQ-OPT", 0.88, 0.92)
    with patch.object(s, '_close_position') as cp:
        s._check_monitor_exits(dtime(14, 1))
        ok("close called", cp.called)
        ok("reason=HARD_TIME_STOP", cp.call_args[0][0] == "HARD_TIME_STOP")


def t16():
    print("\n═══ 16: Post-Trade — Loss Streak Increment ═══")
    s = make()
    s.loss_streak = 1; s.current_allocation = 10000.0
    s.entry_price = 2.00; s.actual_position_size = 5; s.position_open = True
    s.traded_today = True  # Entry normally sets this
    s.trade_direction = "CALL"; s._current_trade_id = "T-1"
    s._total_commission = 3.25
    # Exit at $1.50 → PnL = (1.50-2.00)*100*5 = -250 - commission
    event = MFE(coid="O-EXIT", lpx=1.50, lqty=5.0, side="SELL")
    s._pending_exit_orders.add("O-EXIT")
    s._on_exit_fill(event)
    ok("loss_streak=2", s.loss_streak == 2, f"got {s.loss_streak}")
    ok("allocation decreased", s.current_allocation < 10000.0, f"got {s.current_allocation}")
    ok("position_open=False", not s.position_open)
    ok("traded_today still True (block re-entry)", s.traded_today is not False)  # traded_today unchanged


def t17():
    print("\n═══ 17: Post-Trade — Win Resets Streak ═══")
    s = make()
    s.loss_streak = 3; s.current_allocation = 8000.0
    s.entry_price = 2.00; s.actual_position_size = 5; s.position_open = True
    s.trade_direction = "CALL"; s._current_trade_id = "T-2"
    s._total_commission = 3.25
    # Exit at $3.00 → PnL = (3-2)*100*5 = 500 - commission
    event = MFE(coid="O-EXIT2", lpx=3.00, lqty=5.0, side="SELL")
    s._pending_exit_orders.add("O-EXIT2")
    s._on_exit_fill(event)
    ok("loss_streak=0", s.loss_streak == 0)
    ok("allocation increased", s.current_allocation > 8000.0, f"got {s.current_allocation}")


def t18():
    print("\n═══ 18: Post-Trade — Allocation Updated Correctly ═══")
    s = make()
    s.loss_streak = 0; s.current_allocation = 10000.0
    s.entry_price = 2.00; s.actual_position_size = 10; s.position_open = True
    s.trade_direction = "CALL"; s._current_trade_id = "T-3"
    s._total_commission = 6.50  # 10 * 0.65
    # Exit at $2.50 → raw PnL = (2.50-2.00)*100*10 = 500
    # Net PnL = 500 - 6.50 - exit_comm
    event = MFE(coid="O-EXIT3", lpx=2.50, lqty=10.0, side="SELL", comm=6.50)
    s._pending_exit_orders.add("O-EXIT3")
    s._on_exit_fill(event)
    expected_net = 500.0 - 6.50 - 6.50  # entry + exit comm
    expected_alloc = 10000.0 + expected_net
    ok(f"allocation={expected_alloc:.2f}", abs(s.current_allocation - expected_alloc) < 0.01,
       f"got {s.current_allocation}")


def t19():
    print("\n═══ 19: State Persistence Round-Trip ═══")
    s = make()
    s.loss_streak = 2; s.current_allocation = 9500.0
    s.traded_today = True; s.opening_price = 500.0
    s.position_open = True; s.entry_price = 2.15
    s.trade_direction = "CALL"; s.actual_position_size = 5
    s.current_option_id = "QQQ-0DTE-500C"
    state = s.get_state()
    ok("loss_streak in state", state["loss_streak"] == 2)
    ok("allocation in state", state["current_allocation"] == 9500.0)
    ok("traded_today in state", state["traded_today"] is True)
    ok("entry_price in state", state["entry_price"] == 2.15)
    # Restore to fresh instance
    s2 = make()
    s2.cache.instrument.return_value = None  # No instrument in cache
    s2.set_state(state)
    ok("loss_streak restored", s2.loss_streak == 2)
    ok("allocation restored", s2.current_allocation == 9500.0)
    ok("entry_price restored", s2.entry_price == 2.15)
    ok("trade_direction restored", s2.trade_direction == "CALL")


def t20():
    print("\n═══ 20: Overnight Position Preserved ═══")
    s = make()
    s.position_open = True; s.entry_price = 2.50
    s.trade_direction = "PUT"; s.actual_position_size = 3
    s.current_option_id = "QQQ-OPT"; s._current_trade_id = "T-4"
    s.current_trading_day = date(2026, 2, 28)
    clk(s, y=2026, mo=3, d=1)
    s._reset_daily_state(date(2026, 3, 1))
    ok("position_open kept", s.position_open is True)
    ok("entry_price kept", s.entry_price == 2.50)
    ok("trade_direction kept", s.trade_direction == "PUT")
    ok("actual_position_size kept", s.actual_position_size == 3)
    ok("traded_today reset (allows monitoring)", not s.traded_today)


# ═══ RUN ═════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print(" TFMITH Strategy — Logic Simulation")
    print("=" * 60)
    tests = [t1,t2,t3,t4,t5,t6,t7,t8,t9,t10,t11,t12,t13,t14,t15,t16,t17,t18,t19,t20]
    for fn in tests:
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
