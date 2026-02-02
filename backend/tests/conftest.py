import pytest
from unittest.mock import MagicMock
import sys
import os

# Create dummy classes to avoid MagicMock inheritance issues
class DummyStrategy:
    def __init__(self, config=None, *args, **kwargs):
        self.config = config
        self.log = MagicMock()
        self.instrument_id = MagicMock()
        if config:
             self.strategy_id = config.id
    
    def on_start(self): pass
    def on_stop(self): pass
    def request_instruments(self, *args, **kwargs): pass
    def subscribe_quote_ticks(self, *args, **kwargs): pass
    def unsubscribe_quote_ticks(self, *args, **kwargs): pass
    def close_all_positions(self, *args, **kwargs): pass
    def submit_order(self, *args, **kwargs): pass
    def cancel_order(self, *args, **kwargs): pass

class DummyInstrumentId:
    def __init__(self, id_str):
        self.value = id_str
        
    @staticmethod
    def from_str(id_str):
        return DummyInstrumentId(id_str)
        
    def __str__(self):
        return self.value
    
    def __eq__(self, other):
        return str(self) == str(other)
        
    def __hash__(self):
        return hash(self.value)

# Setup mocks for nautilus_trader
nautilus_mock = MagicMock()
nautilus_mock.trading.strategy.Strategy = DummyStrategy
nautilus_mock.model.identifiers.InstrumentId = DummyInstrumentId
nautilus_mock.__path__ = []

sys.modules['nautilus_trader'] = nautilus_mock
sys.modules['nautilus_trader.model'] = MagicMock()
sys.modules['nautilus_trader.model.data'] = MagicMock()
sys.modules['nautilus_trader.model.enums'] = MagicMock()
sys.modules['nautilus_trader.model.events'] = MagicMock()
sys.modules['nautilus_trader.model.identifiers'] = MagicMock()
sys.modules['nautilus_trader.model.identifiers'].InstrumentId = DummyInstrumentId
sys.modules['nautilus_trader.model.objects'] = MagicMock()
sys.modules['nautilus_trader.model.instruments'] = MagicMock()
sys.modules['nautilus_trader.model.position'] = MagicMock()
sys.modules['nautilus_trader.model.orders'] = MagicMock()
sys.modules['nautilus_trader.common'] = MagicMock()
sys.modules['nautilus_trader.common.enums'] = MagicMock()
sys.modules['nautilus_trader.trading'] = nautilus_mock.trading
sys.modules['nautilus_trader.trading.strategy'] = nautilus_mock.trading.strategy
sys.modules['nautilus_trader.adapters'] = MagicMock()

# Add backend directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

@pytest.fixture
def mock_config():
    config = MagicMock()
    config.id = "test_strategy"
    config.instrument_id = "TEST.SIM"
    # Ensure nested lookups work via get()
    config.parameters = {
        "timezone": "UTC",
        "opening_range_minutes": 15
    }
    # Configure the MagicMock to behave like a dict for get() calls on parameters attribute
    # Note: Using a real dict is easier than mocking .get()
    return config

@pytest.fixture
def mock_integration_manager():
    return MagicMock()

@pytest.fixture
def mock_persistence_manager():
    return MagicMock()
