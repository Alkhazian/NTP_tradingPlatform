# Parquet Data Preparation Guide

This guide explains how to prepare and upload historical Parquet files for backtesting.

## Directory Structure

Historical data should be placed in the dedicated directory:

```
/Users/vortex/Documents/Projects/NTP_tradingPlatform/data/historical_data/
```

Organize your files by instrument:

```
data/
└── historical_data/
    ├── ES/
    │   ├── ES_2020.parquet
    │   ├── ES_2021.parquet
    │   ├── ES_2022.parquet
    │   ├── ES_2023.parquet
    │   └── ES_2024.parquet
    ├── NQ/
    │   ├── NQ_2020.parquet
    │   └── ...
    ├── SPY/
    │   ├── SPY_2020.parquet
    │   └── ...
    └── stocks/
        ├── AAPL_2020.parquet
        └── ...
```

## Parquet File Requirements

### Required Columns

Each Parquet file must contain the following columns:

| Column | Type | Description |
|--------|------|-------------|
| `timestamp` | datetime64 or int64 | Unix timestamp (seconds) or pandas Timestamp |
| `open` | float64 | Opening price |
| `high` | float64 | High price |
| `low` | float64 | Low price |
| `close` | float64 | Closing price |
| `volume` | int64 or float64 | Trading volume |

### Data Format

- **Timeframe**: All data must be **1-minute bars**
- **Timezone**: UTC recommended
- **Sorting**: Data should be sorted by timestamp (ascending)
- **Gaps**: Missing data is acceptable; the backtest engine will handle gaps

### Example Data Structure

```python
import pandas as pd

# Example DataFrame structure
df = pd.DataFrame({
    'timestamp': pd.date_range('2023-01-01', periods=100, freq='1min'),
    'open': [4000.0, 4001.0, ...],
    'high': [4002.0, 4003.0, ...],
    'low': [3999.0, 4000.0, ...],
    'close': [4001.0, 4002.0, ...],
    'volume': [1000, 1500, ...]
})

# Save to Parquet
df.to_parquet('ES_2023.parquet', index=False)
```

## Uploading Data to the Platform

### Method 1: API Upload (Single File)

```bash
curl -X POST http://localhost:8000/backtest/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "file_path": "/Users/vortex/Documents/Projects/NTP_tradingPlatform/data/historical_data/ES/ES_2023.parquet",
    "instrument_id": "ES.FUT",
    "venue": "SIM"
  }'
```

### Method 2: API Upload (Batch Directory)

```bash
curl -X POST http://localhost:8000/backtest/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "directory": "/Users/vortex/Documents/Projects/NTP_tradingPlatform/data/historical_data/ES",
    "instrument_mapping": {
      "ES_*.parquet": "ES.FUT"
    },
    "venue": "SIM"
  }'
```

### Method 3: Batch Upload Multiple Instruments

```bash
curl -X POST http://localhost:8000/backtest/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "directory": "/Users/vortex/Documents/Projects/NTP_tradingPlatform/data/historical_data",
    "instrument_mapping": {
      "ES/*.parquet": "ES.FUT",
      "NQ/*.parquet": "NQ.FUT",
      "SPY/*.parquet": "SPY.NASDAQ"
    },
    "venue": "SIM"
  }'
```

## Verifying Uploaded Data

Check what data is available in the catalog:

```bash
curl http://localhost:8000/backtest/available-data
```

Response example:

```json
{
  "ES.FUT.SIM": {
    "bar_count": 525600,
    "start_date": "2023-01-01T00:00:00",
    "end_date": "2023-12-31T23:59:00"
  },
  "NQ.FUT.SIM": {
    "bar_count": 525600,
    "start_date": "2023-01-01T00:00:00",
    "end_date": "2023-12-31T23:59:00"
  }
}
```

## Instrument ID Format

Use the following format for instrument IDs:

| Instrument Type | Format | Example |
|----------------|--------|---------|
| Futures | `{SYMBOL}.FUT` | `ES.FUT`, `NQ.FUT` |
| Stocks/ETFs | `{SYMBOL}.{EXCHANGE}` | `SPY.NASDAQ`, `AAPL.NASDAQ` |

## Common Issues

### Issue: "Missing required columns"

**Solution**: Ensure your Parquet file has all required columns: `timestamp`, `open`, `high`, `low`, `close`, `volume`

### Issue: "Invalid timestamp format"

**Solution**: Convert timestamps to pandas datetime or Unix timestamp (seconds):

```python
df['timestamp'] = pd.to_datetime(df['timestamp'])
```

### Issue: "Data not appearing in catalog"

**Solution**: 
1. Check the ingestion response for errors
2. Verify file path is correct
3. Ensure instrument_id format is correct

## Best Practices

1. **Split by Year**: Keep separate files per year for easier management
2. **Compress Data**: Parquet files are already compressed, but you can use `compression='snappy'` for better performance
3. **Validate Before Upload**: Check data quality before ingesting
4. **Use Consistent Naming**: Follow a naming convention like `{SYMBOL}_{YEAR}.parquet`
5. **Document Your Data**: Keep a README in each instrument folder noting data source and any preprocessing

## Data Storage Location

After ingestion, data is stored in the Nautilus catalog:

```
data/catalog/
```

This catalog is optimized for fast backtesting and is separate from your original Parquet files.
