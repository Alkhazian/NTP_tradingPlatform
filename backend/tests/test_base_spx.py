import pytest
from unittest.mock import MagicMock, patch
from app.strategies.base_spx import SPXBaseStrategy
from datetime import datetime, time, timedelta
import pytz

class TestSPXBaseStrategy:
    
    @pytest.fixture
    def strategy(self, mock_config, mock_integration_manager, mock_persistence_manager):
        # Allow BaseStrategy.__init__ to run to set up logger, etc.
        # It calls DummyStrategy.__init__ which is fine.
        strategy = SPXBaseStrategy(mock_config, mock_integration_manager, mock_persistence_manager)
        strategy.clock = MagicMock()
        strategy.id = "test_strategy"
        # logger is already created by BaseStrategy.__init__
        return strategy

    def test_process_spx_tick_unified_updates_range(self, strategy):
        """Test that _process_spx_tick_unified updates daily high/low."""
        # Setup
        now = datetime(2023, 1, 1, 9, 35, 0, tzinfo=pytz.UTC)
        strategy.clock.utc_now.return_value = now
        strategy.tz = pytz.UTC
        strategy.market_open_time = time(9, 30)
        strategy.opening_range_minutes = 15
        
        strategy.current_trading_day = now.date()
        
        # Mock tick
        tick = MagicMock()
        strategy.current_spx_price = 4010.0
        
        # Initial state
        strategy.daily_high = 4000.0
        strategy.daily_low = 4000.0
        
        # Action: Process tick with higher price
        strategy._process_spx_tick_unified(tick)
        
        assert strategy.daily_high == 4010.0
        assert strategy.daily_low == 4000.0
        
        # Action: Process tick with lower price
        strategy.current_spx_price = 3990.0
        strategy._process_spx_tick_unified(tick)
        
        assert strategy.daily_high == 4010.0
        assert strategy.daily_low == 3990.0

    def test_range_lock_after_window(self, strategy):
        """Test that range locks after the window implies range_calculated=True."""
        # Setup time AFTER range window (9:30 + 15m = 9:45)
        # Let's say 9:46
        now = datetime(2023, 1, 1, 9, 46, 0, tzinfo=pytz.UTC)
        strategy.clock.utc_now.return_value = now
        strategy.tz = pytz.UTC
        strategy.market_open_time = time(9, 30)
        strategy.opening_range_minutes = 15
        strategy.current_trading_day = now.date()
        
        strategy.daily_high = 4050.0
        strategy.daily_low = 3950.0
        strategy.range_calculated = False
        strategy.current_spx_price = 4000.0
        
        tick = MagicMock()
        
        # Process tick
        strategy._process_spx_tick_unified(tick)
        
        assert strategy.range_calculated is True
        assert strategy.or_high == 4050.0
        assert strategy.or_low == 3950.0
        
    def test_find_option_by_premium_initiates_search(self, strategy):
        """Test that find_option_by_premium generates a search ID and calls callback."""
        strategy.search_option_chain = MagicMock()
        strategy.request_instruments = MagicMock()
        
        # KEY FIX: Set SPX price so it proceeds
        strategy.current_spx_price = 4000.0
        
        callback = MagicMock()
        strategy.find_option_by_premium(
            target_premium=4.0,
            option_kind=MagicMock(), # Mock OptionKind
            callback=callback
        )
        
        assert strategy.request_instruments.called
        # Check that it stored the search request
        assert len(strategy._premium_searches) == 1
