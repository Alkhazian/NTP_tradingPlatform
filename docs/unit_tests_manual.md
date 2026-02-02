# Unit Testing Infrastructure Manual

## Overview

We have established a robust unit testing infrastructure that allows testing trading strategies in isolation, without requiring a live NautilusTrader engine or Redis/IB connection.

This is achieved through:
1.  **Docker Integration**: Tests run inside the `backend` container where the environment matches production.
2.  **Mocking Framework**: A `conftest.py` file provides shared mocks for complex Nautilus objects.

## Current Coverage

| Component | Coverage | Key Validations |
|-----------|----------|-----------------|
| **BaseStrategy** | 100% Core Methods | Initialization, Lifecycle (start/stop), State Persistence (`save_state`), Bracket Order Validation. |
| **SPXBaseStrategy** | High | SPX Tick Processing, Opening Range (OR) Calculation, Option Search Initiation. |
| **SPX15MinRange** | High | Bidirectional Signal Logic, Cross-Invalidation (Low breached → Bullish invalid), Entry Execution (`request_instruments`), Cutoff Time safety. |
| **ORB Strategies** | Maintenance | Existing breakout triggers verified against new infrastructure. |
| **RedisManager** | High | Connection handling, Publish/Subscribe mocking (async). |
| **DrawdownRecorder** | High | State tracking logic, SQLite persistence (in-memory/temp file). |
| **TradeRecorder** | High | Trade lifecycle (start/close), Statistics calculation (Win Rate, PnL). |

## Helper Scripts

### 1. Running All Tests
To run the full test suite, use the following command from the root of the repository:

```bash
docker compose exec backend python -m pytest tests/ -v
```

### 2. Running Specific Tests
To run a specific test file:
```bash
docker compose exec backend python -m pytest tests/test_spx_range.py -v
```

To run a specific test function (e.g., `test_bullish_breakout_signal`):
```bash
docker compose exec backend python -m pytest tests/test_spx_range.py::TestSPX15MinRangeStrategy::test_bullish_breakout_signal -v
```

## Structure

```text
backend/
├── tests/
│   ├── conftest.py          # CENTRAL HUB: All mocks and fixtures live here
│   ├── test_base.py         # Tests for BaseStrategy
│   ├── test_base_spx.py     # Tests for SPXBaseStrategy
│   ├── test_spx_range.py    # Tests for SPX15MinRangeStrategy
│   └── test_orb_strategies.py # Tests for ORB Strategies
```

## How to Add New Tests

### 1. Create a Test File
Create a new file in `backend/tests/`, e.g., `test_my_strategy.py`.

### 2. Use the Shared Fixture
Always use the `strategy` fixture defined in `conftest.py`. This fixture handles all the heavy lifting of mocking `StrategyConfig`, `IntegrationManager`, and `PersistenceManager`.

```python
import pytest
from unittest.mock import MagicMock
from app.strategies.implementations.MyNewStrategy import MyNewStrategy

class TestMyNewStrategy:
    
    @pytest.fixture
    def strategy(self, mock_config, mock_integration_manager, mock_persistence_manager):
        # 1. Customize config if needed
        mock_config.parameters["my_param"] = 100
        
        # 2. Instantiate your strategy
        strategy = MyNewStrategy(mock_config, mock_integration_manager, mock_persistence_manager)
        
        # 3. Mock time
        strategy.clock = MagicMock()
        
        # 4. Initialize (important!)
        strategy.on_start_safe()
        
        return strategy

    def test_my_logic(self, strategy):
        # Setup
        strategy.clock.utc_now.return_value = ...
        
        # Action
        strategy.on_quote_tick_safe(...)
        
        # Assertion
        assert strategy.some_state == "expected_value"
```

## Common Issues & Fixes

### "AttributeError: 'DummyStrategy' object has no attribute 'X'"
The `DummyStrategy` in `conftest.py` mocks the `nautilus_trader.trading.strategy.Strategy` base class. If your strategy calls a method from the base class that isn't mocked (e.g., `cancel_all_orders`), you need to add it to `DummyStrategy` in `conftest.py`.

### "PydanticUserError" or Config Issues
Ensure your `StrategyConfig` uses `model_config = ConfigDict(extra="allow")` instead of the old `class Config`.

### "ModuleNotFoundError"
Always run pytest as a module: `python -m pytest ...`. This ensures the proper `sys.path` is set to include the `app` directory.

## Mocking Time
We use `strategy.clock` which is a `MagicMock`.
- To set the current time: `strategy.clock.utc_now.return_value = datetime(...)`
- To verify a timer was set: `strategy.clock.set_time_alert.assert_called_with(...)`
