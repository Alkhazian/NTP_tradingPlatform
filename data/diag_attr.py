
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.model.identifiers import InstrumentId

catalog = ParquetDataCatalog("/app/data/catalog")
instrument_id = InstrumentId.from_str("MES.SIM")
instruments = catalog.instruments(instrument_ids=[instrument_id])
instrument = instruments[0]

print(f"Type: {type(instrument)}")
print("Attributes:")
for attr in dir(instrument):
    if not attr.startswith("_"):
        try:
            val = getattr(instrument, attr)
            print(f"  {attr}: {val} ({type(val)})")
        except:
            pass
