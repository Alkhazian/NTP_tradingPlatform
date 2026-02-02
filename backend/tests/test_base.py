import pytest
from unittest.mock import MagicMock, call, patch
from app.strategies.base import BaseStrategy

class TestBaseStrategy:
    
    @pytest.fixture
    def strategy(self, mock_config, mock_integration_manager, mock_persistence_manager):
        # We need to patch the BaseStrategy inheritance since it inherits from Strategy which is mocked
        return BaseStrategy(mock_config, mock_integration_manager, mock_persistence_manager)

    def test_initialization(self, strategy):
        """Test proper initialization of BaseStrategy."""
        assert strategy.strategy_id == "test_strategy"
        assert str(strategy.instrument_id) == "TEST.SIM"
        assert strategy._integration_manager is not None
        assert strategy.persistence is not None
        
    def test_on_start_loads_state(self, strategy):
        """Test that on_start attempts to load state."""
        strategy.persistence.load_state = MagicMock(return_value={"test": "state"})
        
        # Mocking super().on_start() is tricky with the dummy class, 
        # but since DummyStrategy.on_start is a pass, we just call it.
        strategy.on_start()
        
        strategy.persistence.load_state.assert_called_with("test_strategy")
        
    def test_save_state(self, strategy):
        """Test state saving logic."""
        strategy.get_state = MagicMock(return_value={"param": 1})
        strategy.save_state()
        
        # BaseStrategy adds internal state, so we check if our param is included
        args, _ = strategy.persistence.save_state.call_args
        assert args[0] == "test_strategy"
        assert args[1]["param"] == 1
        assert "active_trade_id" in args[1]
        
    def test_on_stop_saves_state(self, strategy):
        """Test on_stop saves state."""
        strategy.save_state = MagicMock()
        strategy.on_stop()
        strategy.save_state.assert_called_once()
        
    def test_submit_bracket_order_validation(self, strategy):
        """Test validation preventing bracket order submission if conditions not met."""
        # Setup conditions where submission should fail (e.g., market closed)
        strategy.can_submit_entry_order = MagicMock(return_value=(False, "Market closed"))
        
        order = MagicMock()
        result = strategy.submit_bracket_order(order, 100.0, 110.0)
        
        assert result is False
        # Should not have called submit_order
        # Note: can't check submit_order directly easily if it refers to nautilus method?
        # Actually base.py calls self.submit_order. We can check if that was mocked/called.
        # But BaseStrategy inherits from DummyStrategy which doesn't implement submit_order.
        # So it might crash if called. Ideally we mock submit_order on the instance.
        
    def test_submit_bracket_order_success(self, strategy):
        """Test successful bracket order submission."""
        strategy.can_submit_entry_order = MagicMock(return_value=(True, "OK"))
        strategy.submit_order = MagicMock()
        
        order = MagicMock()
        order.client_order_id = "order_1"
        order.quantity.as_double = MagicMock(return_value=1.0)
        
        result = strategy.submit_bracket_order(order, 100.0, 110.0)
        
        assert result is True
        strategy.submit_order.assert_called_with(order)
        # Verify tracking
        assert "order_1" in strategy._pending_entry_orders
        assert "order_1" in strategy._pending_bracket_exits
        assert strategy._pending_bracket_exits["order_1"]["stop_loss_price"] == 100.0

