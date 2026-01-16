
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
import inspect

engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig()))

print("BacktestEngine.add_venue signature:")
try:
    # Cython methods might not show full signature with inspect.signature
    print(inspect.signature(engine.add_venue))
except Exception as e:
    print(f"Error with inspect.signature: {e}")

print("\nListing all attributes of BacktestEngine (to find related methods):")
print([attr for attr in dir(engine) if "venue" in attr.lower()])
