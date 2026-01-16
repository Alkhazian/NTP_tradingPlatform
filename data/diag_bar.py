
import sys
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.data import BarType, BarSpecification
from nautilus_trader.model.enums import BarAggregation, PriceType

print("Starting diag...", flush=True)

try:
    instrument_id = InstrumentId.from_str("MES.SIM")
    bar_spec = BarSpecification.from_str("1-MINUTE-LAST")
    
    print(f"PriceType.LAST: {PriceType.LAST} type: {type(PriceType.LAST)}", flush=True)

    print("Creating BarType without aggregation_source...", flush=True)
    # Try distinct arguments
    bar_type = BarType(
        instrument_id=instrument_id,
        bar_spec=bar_spec,
    )
    print(f"BarType created: {bar_type}", flush=True)
    
except Exception as e:
    print(f"Error without source: {e}", flush=True)

try:
    print("Creating BarType WITH aggregation_source...", flush=True)
    bar_type = BarType(
        instrument_id=instrument_id,
        bar_spec=bar_spec,
        aggregation_source=PriceType.LAST
    )
    print(f"BarType created with source: {bar_type}", flush=True)
except Exception as e:
    print(f"Error with source: {e}", flush=True)

print("Done.", flush=True)
