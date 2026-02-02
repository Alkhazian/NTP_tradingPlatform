# ORB Strategies Unit Tests
# Tests for breakout detection and SL/TP recalculation logic

import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, time, timedelta
from decimal import Decimal
import pytz

# Mock nautilus_trader imports BEFORE importing strategies
import sys
from unittest.mock import MagicMock

# Create a dummy Strategy class to avoid MagicMock inheritance issues
class DummyStrategy:
    def __init__(self, config=None, *args, **kwargs):
        self.config = config
        self.log = MagicMock()
    
    def on_start(self): pass
    def on_stop(self): pass
    # Add other lifecycle methods if needed

# Setup basic mocks
nautilus_mock = MagicMock()
nautilus_mock.trading.strategy.Strategy = DummyStrategy
nautilus_mock.__path__ = []

sys.modules['nautilus_trader'] = nautilus_mock
sys.modules['nautilus_trader.model'] = MagicMock()
sys.modules['nautilus_trader.model.data'] = MagicMock()
sys.modules['nautilus_trader.model.enums'] = MagicMock()
sys.modules['nautilus_trader.model.events'] = MagicMock()
sys.modules['nautilus_trader.model.identifiers'] = MagicMock()
sys.modules['nautilus_trader.model.objects'] = MagicMock()
sys.modules['nautilus_trader.model.instruments'] = MagicMock()
sys.modules['nautilus_trader.model.position'] = MagicMock()
sys.modules['nautilus_trader.model.orders'] = MagicMock()
sys.modules['nautilus_trader.common'] = MagicMock()
sys.modules['nautilus_trader.common.enums'] = MagicMock()
sys.modules['nautilus_trader.trading'] = nautilus_mock.trading
sys.modules['nautilus_trader.trading.strategy'] = nautilus_mock.trading.strategy
sys.modules['nautilus_trader.adapters'] = MagicMock()

# Now import the strategies
# Adjust the path to ensure we can import from app
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.strategies.implementations.ORB_15_Long_Call import Orb15MinLongCallStrategy
from app.strategies.implementations.ORB_15_Long_Put import Orb15MinLongPutStrategy
from app.strategies.config import StrategyConfig


class TestORBBreakoutDetection:
    """Test suite for ORB breakout detection logic."""
    
    @pytest.fixture
    def mock_config(self):
        config = Mock(spec=StrategyConfig)
        config.parameters = {
            "opening_range_minutes": 15,
            "target_option_price": 4.0,
            "stop_loss_percent": 30.0,
            "take_profit_dollars": 50.0,
            "cutoff_time_hour": 15
        }
        config.order_size = 1
        config.id = "test_orb_strategy"
        config.instrument_id = "SPX"
        return config
    
    @pytest.fixture
    def call_strategy(self, mock_config):
        strategy = Orb15MinLongCallStrategy(mock_config)
        strategy.logger = Mock()
        strategy.clock = Mock()
        strategy.or_high = 4000.0
        strategy.or_low = 3980.0
        strategy.daily_high = 4005.0 # Wick
        strategy.range_calculated = True
        strategy.breakout_detected = False
        strategy.entry_attempted_today = False
        # Mock internal methods
        strategy.is_opening_range_complete = Mock(return_value=True)
        strategy._can_enter = Mock(return_value=(True, "OK"))
        strategy._prepare_entry = Mock()
        return strategy

    @pytest.fixture
    def put_strategy(self, mock_config):
        strategy = Orb15MinLongPutStrategy(mock_config)
        strategy.logger = Mock()
        strategy.clock = Mock()
        strategy.or_high = 4000.0
        strategy.or_low = 3980.0
        strategy.daily_low = 3975.0 # Wick
        strategy.range_calculated = True
        strategy.breakout_detected = False
        strategy.entry_attempted_today = False
        # Mock internal methods
        strategy.is_opening_range_complete = Mock(return_value=True)
        strategy._can_enter = Mock(return_value=(True, "OK"))
        strategy._prepare_entry = Mock()
        return strategy
    
    def test_call_breakout_triggers_on_minute_close_above_or_high(self, call_strategy):
        """Verify Call strategy triggers when minute close > OR High."""
        close_price = 4001.0  # Above 4000.0
        
        call_strategy.on_minute_closed(close_price)
        
        call_strategy.logger.info.assert_any_call(f"Breakout detected on minute close: {close_price:.2f} > {call_strategy.or_high:.2f}")
        assert call_strategy.breakout_detected is True
        call_strategy._prepare_entry.assert_called_once()
        
    def test_call_breakout_ignores_wick_if_minute_close_below_high(self, call_strategy):
        """Verify Call strategy ignores daily_high (wick) if minute close is below OR High."""
        # daily_high is 4005.0 (set in fixture), but close_price is 3999.0
        close_price = 3999.0
        
        call_strategy.on_minute_closed(close_price)
        
        assert call_strategy.breakout_detected is False
        call_strategy._prepare_entry.assert_not_called()
        
    def test_put_breakout_triggers_on_minute_close_below_or_low(self, put_strategy):
        """Verify Put strategy triggers when minute close < OR Low."""
        close_price = 3979.0 # Below 3980.0
        
        put_strategy.on_minute_closed(close_price)
        
        put_strategy.logger.info.assert_any_call(f"Breakout detected on minute close: {close_price:.2f} < {put_strategy.or_low:.2f}")
        assert put_strategy.breakout_detected is True
        put_strategy._prepare_entry.assert_called_once()


class TestSLTPRecalculation:
    """Test suite for SL/TP recalculation on fill."""
    
    @pytest.fixture
    def call_strategy(self):
        config = Mock(spec=StrategyConfig)
        config.parameters = {
            "stop_loss_percent": 30.0,
            "take_profit_dollars": 50.0
        }
        config.order_size = 1
        config.id = "test_orb_strategy_sl_tp"
        config.instrument_id = "SPX"
        strategy = Orb15MinLongCallStrategy(config)
        strategy.stop_loss_percent = 30.0
        strategy.take_profit_dollars = 50.0
        strategy.logger = Mock()
        strategy._pending_entry_orders = {"order_123"}
        strategy._pending_exit_orders = ["exit_123"]
        strategy.save_state = Mock()
        
        # Initial estimated values (from limit order)
        strategy.stop_loss_price = 2.80 # 4.0 * 0.7
        strategy.take_profit_price = 4.50 # 4.0 + 0.5
        return strategy

    def test_sl_tp_recalculated_on_fill(self, call_strategy):
        """Verify SL/TP are recalculated when fill price differs from order price."""
        # Arrange
        fill_price = 4.20
        event = Mock()
        event.client_order_id = "order_123"
        event.last_px.as_double = Mock(return_value=fill_price)
        
        # Act
        call_strategy.on_order_filled_safe(event)
        
        # Assert
        expected_sl = 4.20 * (1 - 0.30)  # 2.94
        expected_tp = 4.20 + 0.50        # 4.70
        
        assert call_strategy.entry_price == fill_price
        assert call_strategy.stop_loss_price == pytest.approx(expected_sl)
        assert call_strategy.take_profit_price == pytest.approx(expected_tp)
        call_strategy.save_state.assert_called()


class TestSoftwareSLFallback:
    """Test software SL executes correctly when broker SL is cancelled."""
    
    @pytest.fixture
    def call_strategy_with_option(self):
        """Setup Call strategy with an active option position."""
        config = Mock(spec=StrategyConfig)
        config.parameters = {
            "stop_loss_percent": 30.0,
            "take_profit_dollars": 50.0
        }
        config.order_size = 1
        config.id = "test_orb_strategy_sl"
        config.instrument_id = "SPX"
        strategy = Orb15MinLongCallStrategy(config)
        strategy.logger = Mock()
        
        # Simulate active position on an option (not SPX)
        strategy.active_option_id = Mock()
        strategy.active_option_id.__str__ = Mock(return_value="SPXW260202C06985000.CBOE")
        
        # Enable software SL (simulates broker SL was cancelled)
        strategy._software_sl_enabled = True
        strategy._software_sl_price = 2.38
        
        # Mock close_strategy_position to verify it's called correctly
        strategy.close_strategy_position = Mock()
        
        return strategy

    @pytest.fixture
    def put_strategy_with_option(self):
        """Setup Put strategy with an active option position."""
        config = Mock(spec=StrategyConfig)
        config.parameters = {
            "stop_loss_percent": 30.0,
            "take_profit_dollars": 50.0
        }
        config.order_size = 1
        config.id = "test_orb_put_strategy_sl"
        config.instrument_id = "SPX"
        strategy = Orb15MinLongPutStrategy(config)
        strategy.logger = Mock()
        
        # Simulate active position on an option (not SPX)
        strategy.active_option_id = Mock()
        strategy.active_option_id.__str__ = Mock(return_value="SPXW260202P06905000.CBOE")
        
        # Enable software SL (simulates broker SL was cancelled)
        strategy._software_sl_enabled = True
        strategy._software_sl_price = 2.38
        
        # Mock close_strategy_position to verify it's called correctly
        strategy.close_strategy_position = Mock()
        
        return strategy
    
    def test_call_software_sl_uses_active_option_id(self, call_strategy_with_option):
        """Verify Call strategy software SL uses active_option_id, not instrument_id."""
        strategy = call_strategy_with_option
        
        # Trigger software SL with price below SL level
        strategy._execute_software_sl(current_price=2.00)
        
        # Verify close was called with override_instrument_id
        strategy.close_strategy_position.assert_called_once()
        call_args = strategy.close_strategy_position.call_args
        assert call_args.kwargs.get('reason') == "SOFTWARE_STOP_LOSS"
        assert call_args.kwargs.get('override_instrument_id') == strategy.active_option_id
    
    def test_put_software_sl_uses_active_option_id(self, put_strategy_with_option):
        """Verify Put strategy software SL uses active_option_id, not instrument_id."""
        strategy = put_strategy_with_option
        
        # Trigger software SL with price below SL level
        strategy._execute_software_sl(current_price=2.00)
        
        # Verify close was called with override_instrument_id
        strategy.close_strategy_position.assert_called_once()
        call_args = strategy.close_strategy_position.call_args
        assert call_args.kwargs.get('reason') == "SOFTWARE_STOP_LOSS"
        assert call_args.kwargs.get('override_instrument_id') == strategy.active_option_id
    
    def test_software_sl_not_triggered_when_price_above_sl(self, call_strategy_with_option):
        """Verify software SL does NOT trigger when price is above SL level."""
        strategy = call_strategy_with_option
        
        # Price is above SL level ($2.38)
        strategy._execute_software_sl(current_price=3.00)
        
        # Verify close was NOT called
        strategy.close_strategy_position.assert_not_called()
        # SL should still be enabled for next check
        assert strategy._software_sl_enabled is True
    
    def test_software_sl_disabled_after_trigger(self, call_strategy_with_option):
        """Verify software SL is disabled after triggering to prevent multiple exits."""
        strategy = call_strategy_with_option
        
        # Trigger software SL
        strategy._execute_software_sl(current_price=2.00)
        
        # SL should now be disabled
        assert strategy._software_sl_enabled is False
        
        # Second call should not trigger another close
        strategy._execute_software_sl(current_price=1.50)
        assert strategy.close_strategy_position.call_count == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
