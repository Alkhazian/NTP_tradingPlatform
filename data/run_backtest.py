
import os
import sys
import pandas as pd
from datetime import datetime

# Add /app to sys.path to find 'app' package
sys.path.append("/app")

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.config import BacktestEngineConfig, BacktestVenueConfig
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.config import LoggingConfig
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.analysis.tearsheet import create_tearsheet
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.objects import Money

# Import our strategy
from app.strategies.implementations.MES_ORB_strategy import MesOrbStrategy
from nautilus_trader.config import StrategyConfig as NautilusStrategyConfig

class MesOrbBacktestConfig(NautilusStrategyConfig, frozen=False):
    instrument_id: str
    parameters: dict
    id: str = ""
    order_size: float = 1.0
    enabled: bool = True

def run_backtest():
    # Paths
    catalog_path = "/app/data/catalog"
    results_dir = "/app/data/backtesting"
    os.makedirs(results_dir, exist_ok=True)
    
    strategy_name = "MES_ORB"
    instrument_id_str = "MES.SIM"
    
    # 1. Setup Catalog
    catalog = ParquetDataCatalog(catalog_path)
    instrument_id = InstrumentId.from_str(instrument_id_str)
    instruments = catalog.instruments(instrument_ids=[instrument_id])
    if not instruments:
        print(f"Error: Instrument {instrument_id_str} not found in catalog.")
        return
    instrument = instruments[0]
    
    # 2. Engine Configuration
    engine_config = BacktestEngineConfig(
        logging=LoggingConfig(log_level="INFO"),
    )
    engine = BacktestEngine(config=engine_config)

    # 3. Venue Configuration
    engine.add_venue(
        venue=Venue("SIM"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(100_000, USD)],
        base_currency=USD,
        bar_execution=True,
        bar_adaptive_high_low_ordering=True,
    )
    engine.add_instrument(instrument)

    # 4. Strategy Configuration
    strategy_config = MesOrbBacktestConfig(
        strategy_id="mes-orb-backtest",
        instrument_id=instrument_id_str,
        id="mes-orb-backtest",
        order_size=1.0,
        enabled=True,
        parameters={
            "or_period_minutes": 15,
            "atr_period": 14,
            "or_atr_multiplier": 0.5,
            "initial_stop_atr_multiplier": 1.25,
            "trailing_stop_atr_multiplier": 3.0,
            "ema_period": 200,
            "adx_period": 14,
            "adx_threshold": 20
        }
    )
    strategy = MesOrbStrategy(config=strategy_config)
    engine.add_strategy(strategy)

    # 5. Load and Add Data
    print(f"Loading data for {instrument_id_str}...")
    start_dt = pd.Timestamp("2020-01-01", tz="UTC")
    end_dt = pd.Timestamp("2026-01-31", tz="UTC")
    
    bars = catalog.bars(
        instrument_ids=[instrument_id],
        start_time=start_dt,
        end_time=end_dt,
    )
    
    if not bars:
        print("No bars found for the specified range.")
        return
    
    print(f"Adding {len(bars)} bars to engine...")
    engine.add_data(bars)

    # 6. Execute Backtest
    print(f"Running backtest for {strategy_name}...")
    engine.run()
    
    # 7. Reporting
    print(f"Generating reports in {results_dir}...")
    
    # a. Summary Statistics
    summary = engine.trader.history.summary()
    summary_path = os.path.join(results_dir, f"{strategy_name}_summary.txt")
    with open(summary_path, "w") as f:
        f.write(str(summary))
    print(f"Summary saved to {summary_path}")

    # b. Interactive Tearsheet
    try:
        tearsheet_path = os.path.join(results_dir, f"{strategy_name}_results.html")
        create_tearsheet(
            engine=engine,
            output_path=tearsheet_path,
        )
        print(f"Tearsheet saved to {tearsheet_path}")
    except ImportError:
        print("Skipping tearsheet generation: plotly not installed.")
    except Exception as e:
        print(f"Failed to generate tearsheet: {e}")
    
    # c. List of Trades
    trades = engine.trader.history.trades()
    if trades:
        trades_df = pd.DataFrame([
            {
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
                "instrument_id": str(t.instrument_id),
                "side": t.side_opened.name,
                "quantity": float(t.quantity),
                "entry_price": float(t.entry_price),
                "exit_price": float(t.exit_price),
                "pnl": float(t.pnl_realized),
                "pnl_percentage": t.pnl_realized_percentage,
            } for t in trades
        ])
        trades_path = os.path.join(results_dir, f"{strategy_name}_trades.csv")
        trades_df.to_csv(trades_path, index=False)
        print(f"Trade list saved to {trades_path}")
    else:
        print("No trades were executed during the backtest.")

    # Properly dispose engine
    engine.dispose()

if __name__ == "__main__":
    run_backtest()
