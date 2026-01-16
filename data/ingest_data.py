
import os
import glob
import pandas as pd
import numpy as np
from nautilus_trader.model.data import Bar, BarType, BarSpecification
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.enums import BarAggregation, PriceType
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.catalog import ParquetDataCatalog

def ingest_data():
    # Paths relative to the container structure (/app/data is mounted)
    # If this script is in /app/data, we can reference local dirs
    
    # We assume running from /app (WORKDIR) or /app/data
    # Use absolute paths in container for safety
    base_data_dir = "/app/data"
    
    # Check if we are in the container
    if not os.path.exists(base_data_dir):
         # Fallback for local testing (if env matches)
         base_data_dir = "/root/ntp-remote/data"

    data_dir = os.path.join(base_data_dir, "historical_data/ES")
    catalog_path = os.path.join(base_data_dir, "catalog")
    
    print(f"Data Dir: {data_dir}")
    print(f"Catalog Path: {catalog_path}")
    
    # Ensure catalog directory exists
    os.makedirs(catalog_path, exist_ok=True)
    
    catalog = ParquetDataCatalog(catalog_path)
    
    # Define instrument ID for backtesting (Continuous)
    instrument_id = InstrumentId.from_str("MES.SIM")
    
    # Define BarType
    bar_spec = BarSpecification.from_str("1-MINUTE-LAST")
    bar_type = BarType(
        instrument_id=instrument_id,
        bar_spec=bar_spec,
    )
    
    files = sorted(glob.glob(os.path.join(data_dir, "ES_*.parquet")))
    if not files:
        print(f"No parquet files found in {data_dir}")
        # Try finding recursively or check path
        print(f"Contents of {os.path.dirname(data_dir)}:")
        try:
            print(os.listdir(os.path.dirname(data_dir)))
        except:
            pass
        return

    print(f"Found {len(files)} files: {files}")

    for file_path in files:
        print(f"Processing {file_path}...")
        try:
            df = pd.read_parquet(file_path)
            
            # Normalize column names: strip whitespace and lowercase
            df.columns = [str(c).lower().strip() for c in df.columns]
            print(f"Columns: {df.columns.tolist()}")

            # Handle index / timestamp
            if isinstance(df.index, pd.DatetimeIndex):
                print("Index is already DatetimeIndex")
            elif 'timestamp' in df.columns:
                df.set_index('timestamp', inplace=True)
            elif 'datetime' in df.columns:
                df.set_index('datetime', inplace=True)
            elif 'time' in df.columns:
                df.set_index('time', inplace=True)
            elif 'date' in df.columns:
                df.set_index('date', inplace=True)
            else:
                # Try to find any column with 'time' or 'date' in it
                found = False
                for col in df.columns:
                    if 'time' in col or 'date' in col:
                        print(f"Auto-detected time column: {col}")
                        df.set_index(col, inplace=True)
                        found = True
                        break
                if not found:
                    print(f"CRITICAL: Could not find timestamp column in {file_path}. Columns: {df.columns.tolist()}")
                    continue
            
            # Ensure index is datetime
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)
            
            # Sort and drop duplicates to prevent 'intervals not disjoint' issues
            df.sort_index(inplace=True)
            df = df[~df.index.duplicated(keep='first')]
            
            print(f"Index head: {df.index[:2]}")

            # 1. Localize to NYC if naive
            if df.index.tz is None:
                df.index = df.index.tz_localize('America/New_York')
            else:
                df.index = df.index.tz_convert('America/New_York')
            
            # 2. Shift to Close Time (Add 1 minute)
            df.index = df.index + pd.Timedelta(minutes=1)
            
            # 3. Convert to UTC
            df.index = df.index.tz_convert('UTC')
            print(f"UTC Index head: {df.index[:2]}")
            print(f"Sample ts.value: {df.index[0].value}")
            
            print(f"Converting {len(df)} rows to Bars...")
            print(f"BarType: {bar_type}")
            
            bars = []
            for ts, row in df.iterrows():
                try:
                    # ts.value is numpy int64, convert to python int
                    ts_ns = int(ts.value)
                    bar = Bar(
                        bar_type=bar_type,
                        open=Price(float(row['open']), 2),
                        high=Price(float(row['high']), 2),
                        low=Price(float(row['low']), 2),
                        close=Price(float(row['close']), 2),
                        volume=Quantity(float(row['volume']), 0),
                        ts_event=ts_ns, 
                        ts_init=ts_ns,
                    )
                    bars.append(bar)
                except KeyError as ke:
                    print(f"Missing column: {ke}. Available: {df.columns}")
                    raise
            
            print(f"Writing {len(bars)} bars to catalog...")
            catalog.write_data(bars)
            print("Done.")

        except Exception as e:
            print(f"Error processing {file_path}: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    ingest_data()
