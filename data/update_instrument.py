
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import FuturesContract
import pandas as pd

catalog = ParquetDataCatalog("/app/data/catalog")

d = {
    'type': 'FuturesContract',
    'id': 'MES.SIM',
    'raw_symbol': 'MES',
    'symbol': 'MES',
    'asset_class': 'INDEX',
    'instrument_class': 'FUTURE',
    'exchange': 'SIM',
    'currency': 'USD',
    'quote_currency': 'USD',
    'underlying': 'MES',
    'is_inverse': False,
    'price_precision': 2,
    'price_increment': '0.25',
    'size_precision': 0,
    'size_increment': '1',
    'multiplier': '5',
    'lot_size': '1',
    'max_quantity': None,
    'min_quantity': '1',
    'max_notional': None,
    'min_notional': None,
    'max_price': None,
    'min_price': None,
    'margin_init': '0',
    'margin_maint': '0',
    'maker_fee': '0',
    'taker_fee': '0',
    'ts_event': 0,
    'ts_init': 0,
    'tick_scheme_name': None,
    'info': None,
    'activation_ns': pd.Timestamp("2020-01-01", tz="UTC").value,
    'expiration_ns': pd.Timestamp("2030-01-01", tz="UTC").value,
}

print("Creating instrument from comprehensive dict...")

try:
    new_instrument = FuturesContract.from_dict(d)
    print(f"New instrument: {new_instrument}")
    
    catalog.write_data([new_instrument])
    print("Successfully updated catalog.")
except Exception as e:
    print(f"Failed: {e}")
    import traceback
    traceback.print_exc()

print("Done.")
