# How to Add a New Strategy

This guide outlines the steps to create and register a new trading strategy in the NautilusTrader-based backend.

## 1. Create the Strategy Class

Create a new python file in `backend/app/strategies/implementations/`. For example, `my_new_strategy.py`.

Your class must inherit from `BaseStrategy` (or `nautilus_trader.trading.strategy.Strategy` if you are building from scratch, but `BaseStrategy` provides helpful integration).

```python
from ..base import BaseStrategy
from ..config import StrategyConfig

class MyNewStrategy(BaseStrategy):
    def on_start(self):
        self.logger.info("My Strategy Starting")
        
    def on_bar(self, bar):
        # Your trading logic here
        pass
```

## 2. Access Parameters

Your strategy can accept specific parameters through the generic `StrategyConfig` object. Access them via the `parameters` dictionary.

```python
    def __init__(self, config: StrategyConfig, integration_manager=None):
        super().__init__(config, integration_manager)
        self.trader_config: StrategyConfig = config
        
        # Access your specific parameters
        self.my_param = config.parameters.get("my_parameter", 10)
        self.another_param = config.parameters.get("another_param", "default")
```

## 3. Implement State Persistence

To ensure your strategy remembers its state (e.g., open positions, last trade time) across restarts, implement the `get_state` and `set_state` methods. `BaseStrategy` automatically handles saving/loading these values.

```python
    def get_state(self) -> Dict[str, Any]:
        return {
            "my_state_var": self.my_state_var,
            "is_position_open": self.is_position_open
        }

    def set_state(self, state: Dict[str, Any]):
        self.my_state_var = state.get("my_state_var", 0)
        self.is_position_open = state.get("is_position_open", False)
```

## 4. Register the Strategy

Open `backend/app/strategies/registry.json` and add your new strategy to the list.

```json
[
    {
        "strategy_type": "SimpleIntervalTrader",
        "module": "app.strategies.implementations.simple_interval_trader",
        "class_name": "SimpleIntervalTrader"
    },
    {
        "strategy_type": "MyNewStrategy",
        "module": "app.strategies.implementations.my_new_strategy",
        "class_name": "MyNewStrategy"
    }
]
```

## 5. Add a Strategy Instance

To actually run a strategy, you must provide its configuration. You can do this in two ways:

1.  **Via Configuration File (Recommended for Persistence)**: Add a JSON file to the `data/strategies/config/` directory on the host machine. The backend will load this on startup.
    
    Example file: `data/strategies/config/MES-Interval-Default.json`
    ```json
    {
        "id": "MES-Interval-Default",
        "name": "My Strategy Instance",
        "enabled": false,
        "instrument_id": "MES.FUT-202603-GLOBEX",
        "strategy_type": "SimpleIntervalTrader",
        "order_size": 1.0,
        "parameters": {
            "buy_interval_minutes": 15,
            "hold_duration_minutes": 2
        }
    }
    ```

2.  **Via the API**: Send a POST request to `/strategies` with the same JSON structure.

## 6. Build and Run

Rebuild the backend container to apply code changes (like new strategy implementations or registry updates):
```bash
docker-compose up --build -d backend
```

Your strategy files in `data/` are persisted on the host and will survive container restarts.

