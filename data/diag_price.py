
from nautilus_trader.model.objects import Price, Quantity

print("Testing Price construction...")
try:
    p = Price(1234.56, precision=2)
    print(f"Price(1234.56, precision=2) success: {p}")
except Exception as e:
    print(f"Price(1234.56, precision=2) error: {e}")

try:
    p = Price(123456, precision=2)
    print(f"Price(123456, precision=2) [int] success: {p}")
except Exception as e:
    print(f"Price(123456, precision=2) [int] error: {e}")

print("Testing Quantity construction...")
try:
    q = Quantity.from_int(100)
    print(f"Quantity.from_int(100) success: {q}")
except Exception as e:
    print(f"Quantity.from_int(100) error: {e}")

try:
    q = Quantity(100.0)
    print(f"Quantity(100.0) success: {q}")
except Exception as e:
    print(f"Quantity(100.0) error: {e}")

print("Done.")
