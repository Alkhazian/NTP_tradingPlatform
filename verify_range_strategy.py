import sys
import os
import pytz
from datetime import datetime
from unittest.mock import MagicMock

# Mock nautilus_trader before importing strategy
sys.modules["nautilus_trader"] = MagicMock()
sys.modules["nautilus_trader.trading"] = MagicMock()
sys.modules["nautilus_trader.model"] = MagicMock()
sys.modules["nautilus_trader.model.data"] = MagicMock()
sys.modules["nautilus_trader.model.enums"] = MagicMock()
sys.modules["nautilus_trader.model.enums"].OptionKind = MagicMock()
sys.modules["nautilus_trader.model.enums"].TimeInForce = MagicMock()
sys.modules["nautilus_trader.model.enums"].OrderStatus = MagicMock()
sys.modules["nautilus_trader.model.identifiers"] = MagicMock()
sys.modules["nautilus_trader.model.instruments"] = MagicMock()
sys.modules["nautilus_trader.model.position"] = MagicMock()
sys.modules["nautilus_trader.model.objects"] = MagicMock()
sys.modules["nautilus_trader.model.orders"] = MagicMock()
sys.modules["nautilus_trader.common"] = MagicMock()
sys.modules["nautilus_trader.common.enums"] = MagicMock()
sys.modules["nautilus_trader.common.enums"].ComponentState = MagicMock()
sys.modules["nautilus_trader.trading.strategy"] = MagicMock()
sys.modules["nautilus_trader.trading.strategy"].Strategy = type('Strategy', (), {'__init__': lambda *args, **kwargs: None})

sys.modules["pydantic"] = MagicMock()
sys.modules["pydantic_settings"] = MagicMock()

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

sys.path.insert(0, os.path.abspath("backend"))

from app.strategies.config import StrategyConfig
from app.strategies.implementations.SPX_Range_Strategy import SPXRangeStrategy

def test_strategy_dte(dte, expected_suffix):
    config = StrategyConfig(
        id=f"test_dte_{dte}", 
        name="Test", 
        enabled=True, 
        instrument_id="^SPX.CBOE",
        strategy_type="SPXRangeStrategy", 
        order_size=1,
        parameters={"dte": dte, "timezone": "US/Eastern", "start_time_str": "09:30:00"}
    )
    
    # Intentionally bypass Base classes to just unit test _initiate_entry_sequence
    strat = object.__new__(SPXRangeStrategy)
    strat.strategy_config = config
    strat.strategy_id = config.id
    strat.id = config.id
    strat.tz = pytz.timezone("America/New_York")
    strat.logger = MagicMock()
    strat.clock = MagicMock()
    
    # Mock date to be Thursday, Jan 15th 2026
    dt = strat.tz.localize(datetime(2026, 1, 15, 10, 0, 0))
    strat.clock.utc_now.return_value = dt.astimezone(pytz.utc)
    
    strat.dte = dte
    strat._signal_direction = 'bearish'
    strat.or_high = 6000.0
    strat.strike_step = 5
    strat.strike_width = 5
    strat.entry_timeout_seconds = 35
    strat._found_legs = {}
    strat.request_instruments = MagicMock()
    
    strat._initiate_entry_sequence()
    
    # The expected instrument IDs list is populated in the method
    expected_contains = expected_suffix
    matches = any(expected_contains in req for req in strat._expected_instrument_ids)
    print(f"Testing DTE={dte}: Expected '{expected_contains}' produced: {strat._expected_instrument_ids} -> {'PASS' if matches else 'FAIL'}")
    return matches

def main():
    print("Verifying DTE configurations for SPXRangeStrategy...\n")
    # Thursday -> dte=0 -> Thursday (15th) => SPXW260115
    pass1 = test_strategy_dte(0, "SPXW260115")
    # Thursday -> dte=1 -> Friday (16th) => SPXW260116
    pass2 = test_strategy_dte(1, "SPXW260116")
    # Thursday -> dte=2 -> Saturday -> Skips to Monday (19th) => SPXW260119
    pass3 = test_strategy_dte(2, "SPXW260119")
    
    if pass1 and pass2 and pass3:
        print("\nAll tests PASSED.")
        sys.exit(0)
    else:
        print("\nSome tests FAILED.")
        sys.exit(1)

if __name__ == "__main__":
    main()
