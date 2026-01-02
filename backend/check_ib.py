try:
    import nautilus_trader.adapters.interactive_brokers.factories as factories
    print("Factories:", dir(factories))
except ImportError as e:
    print("Factories import error:", e)

try:
    import nautilus_trader.adapters.interactive_brokers as ib
    print("IB:", dir(ib))
except ImportError as e:
    print("IB import error:", e)
