
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.data import Bar

catalog = ParquetDataCatalog("/app/data/catalog")
instrument_id = InstrumentId.from_str("MES.SIM")

print(f"Querying bars for {instrument_id}...")
bars = catalog.bars(instrument_ids=[instrument_id])

if bars:
    print(f"Total bars found: {len(bars)}")
    print(f"First bar: {bars[0]}")
    print(f"Last bar: {bars[-1]}")
else:
    print("No bars found in catalog for this instrument.")

print("\nListing all instruments in catalog:")
print(catalog.instruments())
