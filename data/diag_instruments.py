
from nautilus_trader.model.instruments import FuturesContract
import inspect

print("FuturesContract constructor signature:")
try:
    print(inspect.signature(FuturesContract.__init__))
except Exception as e:
    print(f"Error: {e}")

# Try to see if it's a msgspec Struct
import msgspec
try:
    print("msgspec fields:")
    print(msgspec.structs.fields(FuturesContract))
except Exception as e:
    print(f"Error: {e}")
