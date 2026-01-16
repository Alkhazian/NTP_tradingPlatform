
from nautilus_trader.config import StrategyConfig
import msgspec

print("StrategyConfig fields:")
try:
    # Use msgspec to inspect the struct if it's one
    print(msgspec.structs.fields(StrategyConfig))
except Exception as e:
    print(f"Error inspecting with msgspec: {e}")

print("\nTrying to instantiate StrategyConfig with various args...")
try:
    c = StrategyConfig(strategy_id="test")
    print("Success with strategy_id")
except Exception as e:
    print(f"Error with strategy_id: {e}")

try:
    c = StrategyConfig(id="test")
    print("Success with id")
except Exception as e:
    print(f"Error with id: {e}")

try:
    # Some older versions might use 'name' or just positional?
    c = StrategyConfig()
    print("Success with no args")
except Exception as e:
    print(f"Error with no args: {e}")
