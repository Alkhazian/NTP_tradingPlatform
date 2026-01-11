import logging
import os
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
import pandas as pd
import numpy as np

from nautilus_trader.model.identifiers import InstrumentId, Venue, Symbol
from nautilus_trader.model.instruments import Instrument, FuturesContract, Equity
from nautilus_trader.model.objects import Price, Quantity, Money, Currency
from nautilus_trader.model.enums import BarAggregation, PriceType, AssetClass
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.model.data import Bar, BarType, BarSpecification
from nautilus_trader.core.datetime import dt_to_unix_nanos

logger = logging.getLogger(__name__)


class ParquetImporter:
    """
    Service for importing Parquet files into Nautilus catalog for backtesting.
    Handles 1-min bar data for ES, NQ, SPY, and other instruments.
    """

    def __init__(self, catalog_path: str = "data/catalog", historical_data_path: str = "data/historical_data"):
        """
        Initialize the Parquet importer.
        
        Args:
            catalog_path: Path to the Nautilus catalog directory
            historical_data_path: Default path for historical Parquet files
        """
        self.catalog_path = Path(catalog_path)
        self.catalog_path.mkdir(parents=True, exist_ok=True)
        self.historical_data_path = Path(historical_data_path)
        self.historical_data_path.mkdir(parents=True, exist_ok=True)
        self.catalog = ParquetDataCatalog(str(self.catalog_path))
        logger.info(f"Initialized ParquetImporter with catalog at {self.catalog_path}")
        logger.info(f"Default historical data path: {self.historical_data_path}")

    def _resolve_path(self, path: str) -> str:
        """
        Resolve a path, checking if it's relative to historical_data_path.
        
        Args:
            path: File or directory path (absolute or relative)
            
        Returns:
            Absolute path string
        """
        path_obj = Path(path)
        
        # If absolute path, use as-is
        if path_obj.is_absolute():
            return str(path_obj)
        
        # Try relative to historical_data_path first
        historical_path = self.historical_data_path / path
        if historical_path.exists():
            return str(historical_path)
        
        # Fall back to current working directory
        return str(path_obj.resolve())


    def ingest_parquet_file(
        self,
        file_path: str,
        instrument_id: str,
        venue: str = "SIM",
        bar_type: str = "1-MINUTE-LAST",
        timezone: str = "UTC",
    ) -> int:
        """
        Ingest a single Parquet file into the catalog.
        
        Args:
            file_path: Path to the Parquet file (absolute or relative to data/historical_data/)
            instrument_id: Instrument identifier (e.g., "ES.FUT", "SPY.NASDAQ")
            venue: Venue name (default: "SIM" for simulation)
            bar_type: Bar type specification (default: "1-MINUTE-LAST")
            timezone: Timezone of the data (default: "UTC"). Examples: "US/Eastern", "Europe/London"
            
        Returns:
            Number of bars ingested
        """
        try:
            # Resolve path (supports relative paths from historical_data/)
            resolved_path = self._resolve_path(file_path)
            logger.info(f"Ingesting {resolved_path} for {instrument_id} (timezone: {timezone})")
            
            # Read Parquet file
            df = pd.read_parquet(resolved_path)
            
            # Validate required columns (volume is optional)
            required_cols = ['timestamp', 'open', 'high', 'low', 'close']
            missing_cols = [col for col in required_cols if col not in df.columns]
            if missing_cols:
                raise ValueError(f"Missing required columns: {missing_cols}")
            
            # Add volume column with default value if missing
            if 'volume' not in df.columns:
                logger.warning(f"Volume column missing in {file_path}, using default value 0")
                df['volume'] = 0
            
            # Convert timezone if not UTC
            if timezone != "UTC":
                logger.info(f"Converting timestamps from {timezone} to UTC")
                if not isinstance(df['timestamp'].iloc[0], pd.Timestamp):
                    df['timestamp'] = pd.to_datetime(df['timestamp'])
                # Localize to source timezone, then convert to UTC
                df['timestamp'] = df['timestamp'].dt.tz_localize(timezone).dt.tz_convert('UTC')
            
            # Convert to Nautilus Bar objects
            bars = self._dataframe_to_bars(df, instrument_id, venue, bar_type)
            
            # Write to catalog
            self.catalog.write_data(bars)
            
            # Update metadata cache
            if bars:
                self._update_metadata_cache(
                    instrument_id, 
                    len(bars), 
                    min(b.ts_event for b in bars), 
                    max(b.ts_event for b in bars)
                )
            
            logger.info(f"Successfully ingested {len(bars)} bars for {instrument_id}")
            return len(bars)
            
        except Exception as e:
            logger.error(f"Failed to ingest {file_path}: {e}", exc_info=True)
            raise

    def ingest_directory(
        self,
        directory: str,
        instrument_mapping: Dict[str, str],
        venue: str = "SIM",
        timezone: str = "UTC",
        bar_type: str = "1-MINUTE-LAST",
    ) -> Dict[str, int]:
        """
        Ingest all Parquet files from a directory.
        
        Args:
            directory: Path to directory (absolute or relative to data/historical_data/)
            instrument_mapping: Mapping of filename patterns to instrument IDs
                Example: {"ES_*.parquet": "ES.FUT", "SPY_*.parquet": "SPY.NASDAQ"}
            venue: Venue name
            timezone: Timezone of the data (default: "UTC")
            bar_type: Bar type specification (default: "1-MINUTE-LAST")
            
        Returns:
            Dictionary mapping instrument IDs to number of bars ingested
        """
        results = {}
        
        # Resolve directory path
        resolved_dir = self._resolve_path(directory)
        directory_path = Path(resolved_dir)
        
        if not directory_path.exists():
            raise ValueError(f"Directory does not exist: {resolved_dir}")
        
        for pattern, instrument_id in instrument_mapping.items():
            files = list(directory_path.glob(pattern))
            total_bars = 0
            
            for file_path in files:
                try:
                    bars_count = self.ingest_parquet_file(
                        str(file_path),
                        instrument_id,
                        venue,
                        bar_type=bar_type,
                        timezone=timezone
                    )
                    total_bars += bars_count
                except Exception as e:
                    logger.error(f"Skipping {file_path}: {e}")
                    continue
            
            if total_bars > 0:
                results[instrument_id] = total_bars
                logger.info(f"Ingested {total_bars} total bars for {instrument_id}")
        
        return results

    def _dataframe_to_bars(
        self,
        df: pd.DataFrame,
        instrument_id: str,
        venue: str,
        bar_type: str,
    ) -> List[Bar]:
        """
        Convert a pandas DataFrame to Nautilus Bar objects.
        
        Args:
            df: DataFrame with OHLCV data
            instrument_id: Instrument identifier
            venue: Venue name
            bar_type: Bar type specification
            
        Returns:
            List of Nautilus Bar objects
        """
        bars = []
        
        # Parse instrument and bar type
        instrument_id_obj = InstrumentId.from_str(f"{instrument_id}.{venue}")
        
        # Parse bar_type string (e.g., "1-MINUTE-LAST") into components
        parts = bar_type.split('-')
        if len(parts) != 3:
            raise ValueError(f"Invalid bar_type format: {bar_type}. Expected format: 'N-UNIT-PRICETYPE' (e.g., '1-MINUTE-LAST')")
        
        step = int(parts[0])
        unit = parts[1]  # e.g., "MINUTE", "SECOND", "HOUR"
        price_type_str = parts[2]  # e.g., "LAST", "BID", "ASK"
        
        # Map string to BarAggregation enum (time-based)
        aggregation_map = {
            "SECOND": BarAggregation.SECOND,
            "MINUTE": BarAggregation.MINUTE,
            "HOUR": BarAggregation.HOUR,
            "DAY": BarAggregation.DAY,
            "TICK": BarAggregation.TICK,
            "VOLUME": BarAggregation.VOLUME,
        }
        
        # Map string to PriceType enum
        price_type_map = {
            "LAST": PriceType.LAST,
            "BID": PriceType.BID,
            "ASK": PriceType.ASK,
            "MID": PriceType.MID,
        }
        
        aggregation = aggregation_map.get(unit.upper())
        if not aggregation:
            raise ValueError(f"Unsupported bar aggregation unit: {unit}")
        
        price_type = price_type_map.get(price_type_str.upper(), PriceType.LAST)
        
        # Create BarSpecification
        bar_spec = BarSpecification(
            step=step,
            aggregation=aggregation,
            price_type=price_type,
        )
        
        # Create BarType
        bar_type_obj = BarType(
            instrument_id=instrument_id_obj,
            bar_spec=bar_spec,
        )
        
        # Prepare columns as lists for faster iteration
        timestamps = df['timestamp'].values
        opens = df['open'].values
        highs = df['high'].values
        lows = df['low'].values
        closes = df['close'].values
        volumes = df['volume'].values
        
        for i in range(len(df)):
            # Handle timestamp
            ts = timestamps[i]
            if isinstance(ts, pd.Timestamp):
                ts_ns = dt_to_unix_nanos(ts.to_pydatetime())
            elif isinstance(ts, datetime):
                ts_ns = dt_to_unix_nanos(ts)
            elif isinstance(ts, (int, float, np.integer, np.floating)):
                # Assume it's a Unix timestamp (seconds or nanoseconds)
                # If it's very large, assume nanoseconds, otherwise seconds
                if ts > 1e15: 
                    ts_ns = int(ts)
                else:
                    ts_ns = int(ts * 1_000_000_000)
            else:
                # Try to convert if it's something else
                ts_ns = dt_to_unix_nanos(pd.to_datetime(ts).to_pydatetime())
            
            bar = Bar(
                bar_type=bar_type_obj,
                open=Price.from_str(str(opens[i])),
                high=Price.from_str(str(highs[i])),
                low=Price.from_str(str(lows[i])),
                close=Price.from_str(str(closes[i])),
                volume=Quantity.from_str(str(volumes[i])),
                ts_event=ts_ns,
                ts_init=ts_ns,
            )
            bars.append(bar)
        
        return bars

    def _get_metadata_cache_path(self) -> Path:
        """Get the path to the metadata cache file."""
        return self.catalog_path / "metadata_cache.json"

    def _load_metadata_cache(self) -> Dict[str, Dict]:
        """Load the metadata cache from disk."""
        cache_path = self._get_metadata_cache_path()
        if cache_path.exists():
            try:
                import json
                with open(cache_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load metadata cache: {e}")
        return {}

    def _save_metadata_cache(self, cache: Dict[str, Dict]):
        """Save the metadata cache to disk."""
        cache_path = self._get_metadata_cache_path()
        try:
            import json
            with open(cache_path, 'w') as f:
                json.dump(cache, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save metadata cache: {e}")

    def _update_metadata_cache(self, instrument_id: str, bars_count: int, first_ts: int, last_ts: int):
        """Update the metadata cache with new ingestion results."""
        cache = self._load_metadata_cache()
        
        start_date = datetime.fromtimestamp(first_ts / 1_000_000_000)
        end_date = datetime.fromtimestamp(last_ts / 1_000_000_000)
        
        if instrument_id in cache:
            # Update existing entry
            cache[instrument_id]["bar_count"] += bars_count
            existing_start = datetime.fromisoformat(cache[instrument_id]["start_date"])
            existing_end = datetime.fromisoformat(cache[instrument_id]["end_date"])
            
            cache[instrument_id]["start_date"] = min(start_date, existing_start).isoformat()
            cache[instrument_id]["end_date"] = max(end_date, existing_end).isoformat()
        else:
            # Create new entry
            cache[instrument_id] = {
                "bar_count": bars_count,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            }
        
        self._save_metadata_cache(cache)

    def list_available_data(self) -> Dict[str, Dict]:
        """
        List all available data in the catalog using the metadata cache.
        
        Returns:
            Dictionary mapping instrument IDs to metadata (date range, bar count)
        """
        cache = self._load_metadata_cache()
        if cache:
            return cache
            
        # If cache is empty, perform a one-time scan to rebuild it
        logger.info("Metadata cache empty, scanning catalog...")
        try:
            instruments = self.catalog.instruments()
            result = {}
            for instrument in instruments:
                instrument_id = str(instrument.id)
                bars = self.catalog.bars(instrument_ids=[instrument_id])
                if bars:
                    timestamps = [bar.ts_event for bar in bars]
                    start_date = datetime.fromtimestamp(min(timestamps) / 1_000_000_000)
                    end_date = datetime.fromtimestamp(max(timestamps) / 1_000_000_000)
                    result[instrument_id] = {
                        "bar_count": len(bars),
                        "start_date": start_date.isoformat(),
                        "end_date": end_date.isoformat(),
                    }
            
            if result:
                self._save_metadata_cache(result)
            return result
            
        except Exception as e:
            logger.error(f"Failed to list available data: {e}", exc_info=True)
            return {}

    def create_instrument_definition(
        self,
        symbol: str,
        instrument_type: str = "futures",
        venue: str = "SIM",
        **kwargs
    ) -> Instrument:
        """
        Create an instrument definition for the catalog.
        
        Args:
            symbol: Instrument symbol (e.g., "ES", "SPY")
            instrument_type: Type of instrument ("futures" or "equity")
            venue: Venue name
            **kwargs: Additional instrument parameters
            
        Returns:
            Nautilus Instrument object
        """
        venue_obj = Venue(venue)
        instrument_id = InstrumentId.from_str(f"{symbol}.{venue}")
        
        if instrument_type == "futures":
            # Map asset class string to Enum
            ac_str = str(kwargs.get("asset_class", "INDEX")).upper()
            try:
                ac = AssetClass[ac_str]
            except KeyError:
                logger.warning(f"Unknown asset class {ac_str}, defaulting to INDEX")
                ac = AssetClass.INDEX

            # Create futures contract
            instrument = FuturesContract(
                instrument_id=instrument_id,
                raw_symbol=Symbol(symbol),
                asset_class=ac,
                currency=Currency.from_str(str(kwargs.get("currency", "USD"))),
                price_precision=int(kwargs.get("price_precision", 2)),
                price_increment=Price.from_str(str(kwargs.get("price_increment", "0.01"))),
                multiplier=Quantity.from_str(str(kwargs.get("multiplier", "1"))),
                lot_size=Quantity.from_str(str(kwargs.get("lot_size", "1"))),
                underlying=symbol,
                activation_ns=0,
                expiration_ns=int(kwargs.get("expiration_ns", 0)),
                ts_event=0,
                ts_init=0,
                exchange=venue
            )
        elif instrument_type == "equity":
            # Create equity
            instrument = Equity(
                instrument_id=instrument_id,
                raw_symbol=Symbol(symbol),
                currency=Currency.from_str(str(kwargs.get("currency", "USD"))),
                price_precision=int(kwargs.get("price_precision", 2)),
                price_increment=Price.from_str(str(kwargs.get("price_increment", "0.01"))),
                lot_size=Quantity.from_str(str(kwargs.get("lot_size", "1"))),
                ts_event=0,
                ts_init=0,
                exchange=venue
            )
        else:
            raise ValueError(f"Unsupported instrument type: {instrument_type}")
        
        # Write instrument to catalog
        self.catalog.write_data([instrument])
        logger.info(f"Created instrument definition for {instrument_id}")
        
        return instrument
