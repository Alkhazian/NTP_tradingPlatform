
import pandas as pd

try:
    df = pd.read_parquet('/root/ntp-remote/data/historical_data/ES/ES_2020.parquet')
    print("Columns:", df.columns.tolist())
    print("First 5 rows:")
    print(df.head())
    print("Last 5 rows:")
    print(df.tail())
except Exception as e:
    print(e)
