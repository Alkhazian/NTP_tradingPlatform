
import pandas as pd
import os

file_path = "/app/data/historical_data/ES/ES_2020.parquet"
if os.path.exists(file_path):
    df = pd.read_parquet(file_path)
    print(f"Columns: {df.columns.tolist()}")
    print(f"Index type: {type(df.index)}")
    print(f"Index head:\n{df.index[:5]}")
    if not isinstance(df.index, pd.DatetimeIndex):
        # Check if any column is datetime-like
        for col in df.columns:
            print(f"Column '{col}' head: {df[col][:5]}")
else:
    print(f"File not found: {file_path}")
