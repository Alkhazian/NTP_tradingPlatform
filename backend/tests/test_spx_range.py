import pytest
from unittest.mock import MagicMock, patch, call
from app.strategies.implementations.SPX_15Min_Range import SPX15MinRangeStrategy
from datetime import datetime, time
import pytz

class TestSPX15MinRangeStrategy:
    
    @pytest.fixture
    def strategy(self, mock_config, mock_integration_manager, mock_persistence_manager):
        # Configure specific parameters for this strategy
        mock_config.parameters.update({
            "timezone": "UTC",
            "start_time_str": "09:30:00",
            "entry_cutoff_time_str": "12:00:00",
            "stop_loss_multiplier": 2.0,
            "min_credit_amount": 50.0
        })
        
        strategy = SPX15MinRangeStrategy(mock_config, mock_integration_manager, mock_persistence_manager)
        strategy.clock = MagicMock()
        strategy.id = "test_strategy_range"
        
        # Setup common state
        strategy.or_high = 4010.0
        strategy.or_low = 3990.0
        strategy.range_calculated = True
        strategy.spx_instrument = MagicMock()
        
        # Determine TZ aware times for tests
        now = datetime(2023, 1, 1, 10, 0, 0, tzinfo=pytz.UTC)
        strategy.clock.utc_now.return_value = now
        strategy.current_trading_day = now.date()
        
        # Initialize strategy attributes (calls on_start_safe)
        strategy._subscribe_to_spx = MagicMock()
        strategy.on_start_safe()
        
        return strategy

    def test_initialization(self, strategy):
        """Test proper initialization of SPX Range strategy."""
        assert strategy.high_breached is False
        assert strategy.low_breached is False
        assert strategy.stop_loss_multiplier == 2.0

    def test_bullish_breakout_signal(self, strategy):
        """test Bullish breakout: Close > High, Low not breached."""
        strategy.request_instruments = MagicMock()
        strategy.high_breached = False
        strategy.low_breached = False
        
        close_price = 4020.0 # > 4010.0
        strategy.current_spx_price = close_price # Needed for deviation check
        
        strategy.on_minute_closed(close_price)
        
        # Expectation: high_breached set to True, Entry sequence initiated
        assert strategy.high_breached is True
        assert strategy.request_instruments.called
        
    def test_bearish_breakout_signal(self, strategy):
        """Test Bearish breakout: Close < Low, High not breached."""
        strategy.request_instruments = MagicMock()
        strategy.high_breached = False
        strategy.low_breached = False
        
        close_price = 3980.0 # < 3990.0
        strategy.current_spx_price = close_price
        
        strategy.on_minute_closed(close_price)
        
        assert strategy.low_breached is True
        assert strategy.request_instruments.called
        
    def test_signal_invalidation_bullish_invalidated(self, strategy):
        """Test correct invalidation: Close > High but after Low was breached."""
        strategy.low_breached = True
        strategy.high_breached = False
        strategy.request_instruments = MagicMock()
        
        close_price = 4020.0 # > High
        
        strategy.on_minute_closed(close_price)
        
        # Should mark high breached too
        assert strategy.high_breached is True
        
        # BUT should NOT enter trade because low was already breached
        assert not strategy.request_instruments.called

    def test_no_trade_past_cutoff(self, strategy):
        """Test that no signals are processed after cutoff time."""
        # Set time to 13:00 (cutoff is 12:00)
        late_time = datetime(2023, 1, 1, 13, 0, 0, tzinfo=pytz.UTC)
        strategy.clock.utc_now.return_value = late_time
        strategy.entry_cutoff_time = time(12, 0)
        
        strategy.request_instruments = MagicMock()
        
        strategy.on_minute_closed(4020.0) # Breakout
        
        assert not strategy.request_instruments.called

    def test_already_traded_prevents_entry(self, strategy):
        """Test that traded_today flag prevents new entries."""
        strategy.traded_today = True
        strategy.request_instruments = MagicMock()
        
        strategy.on_minute_closed(4020.0) # Breakout
        
        assert not strategy.request_instruments.called
