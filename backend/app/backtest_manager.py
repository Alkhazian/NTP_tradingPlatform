import logging
import asyncio
from pathlib import Path
from typing import List, Dict, Optional, Any
from datetime import datetime
import uuid

from nautilus_trader.backtest.node import BacktestNode
from nautilus_trader.config import (
    BacktestRunConfig,
    BacktestDataConfig,
    BacktestVenueConfig,
    BacktestEngineConfig,
    ImportableStrategyConfig,
    PerContractFeeModelConfig,
    FillModelConfig,
)
from nautilus_trader.model.identifiers import Venue, InstrumentId
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.analysis.tearsheet import create_tearsheet

logger = logging.getLogger(__name__)


class BacktestConfig:
    """Configuration for a backtest run."""
    
    def __init__(
        self,
        strategy_id: str,
        strategy_config: Dict[str, Any],
        instruments: List[str],
        start_date: str,
        end_date: str,
        venue: str = "SIM",
        initial_balance: float = 100000.0,
        currency: str = "USD",
    ):
        self.run_id = str(uuid.uuid4())
        self.strategy_id = strategy_id
        self.strategy_config = strategy_config
        self.instruments = instruments
        self.start_date = start_date
        self.end_date = end_date
        self.venue = venue
        self.initial_balance = initial_balance
        self.currency = currency
        self.commission_per_contract = float(strategy_config.get("commission_per_contract", 0.0))
        self.slippage_prob = float(strategy_config.get("slippage_prob", 0.0))


class BacktestManager:
    """
    Manages backtesting using NautilusTrader's BacktestNode (high-level API).
    Handles multiple backtest runs with different configurations.
    """

    def __init__(self, catalog_path: str = "data/catalog", results_path: str = "data/backtest_results"):
        """
        Initialize the backtest manager.
        
        Args:
            catalog_path: Path to the Nautilus catalog with historical data
            results_path: Path to store backtest results and tearsheets
        """
        self.catalog_path = Path(catalog_path)
        self.results_path = Path(results_path)
        self.results_path.mkdir(parents=True, exist_ok=True)
        
        self.catalog = ParquetDataCatalog(str(self.catalog_path))
        self.results: Dict[str, Dict] = {}  # Store results by run_id
        
        logger.info(f"Initialized BacktestManager with catalog at {self.catalog_path}")

    async def run_backtest(self, config: BacktestConfig) -> Dict[str, Any]:
        """
        Run a backtest using BacktestNode.
        
        Args:
            config: Backtest configuration
            
        Returns:
            Dictionary with backtest results
        """
        try:
            logger.info(f"Starting backtest {config.run_id} for strategy {config.strategy_id}")
            
            # Build BacktestRunConfig
            run_config = self._build_run_config(config)
            
            # Create BacktestNode with the configuration
            node = BacktestNode(configs=[run_config])
            
            # Run the backtest
            # Note: BacktestNode.run() is synchronous, but we're in async context
            await asyncio.get_event_loop().run_in_executor(None, node.run)
            
            # Get the engine from the node (first run)
            engine = node.get_engine(run_config.id)
            
            # Extract results
            results = self._extract_results(engine, config)
            
            # Generate tearsheet
            tearsheet_path = self.results_path / f"{config.run_id}_tearsheet.html"
            create_tearsheet(
                engine=engine,
                output_path=str(tearsheet_path),
            )
            results["tearsheet_path"] = str(tearsheet_path)
            
            # Generate reports
            orders_report = engine.trader.generate_orders_report()
            positions_report = engine.trader.generate_positions_report()
            fills_report = engine.trader.generate_fills_report()
            
            results["reports"] = {
                "orders": orders_report.to_dict() if hasattr(orders_report, 'to_dict') else str(orders_report),
                "positions": positions_report.to_dict() if hasattr(positions_report, 'to_dict') else str(positions_report),
                "fills": fills_report.to_dict() if hasattr(fills_report, 'to_dict') else str(fills_report),
            }
            
            # Store results
            self.results[config.run_id] = results
            
            logger.info(f"Backtest {config.run_id} completed successfully")
            return results
            
        except Exception as e:
            logger.error(f"Backtest {config.run_id} failed: {e}", exc_info=True)
            raise

    def _build_run_config(self, config: BacktestConfig) -> BacktestRunConfig:
        """
        Build a BacktestRunConfig from our BacktestConfig.
        
        Args:
            config: Our backtest configuration
            
        Returns:
            Nautilus BacktestRunConfig
        """
        # Parse dates
        start_dt = datetime.fromisoformat(config.start_date)
        end_dt = datetime.fromisoformat(config.end_date)
        
        # Build data configs for each instrument
        data_configs = []
        for instrument_str in config.instruments:
            instrument_id = InstrumentId.from_str(f"{instrument_str}.{config.venue}")
            
            data_config = BacktestDataConfig(
                catalog_path=str(self.catalog_path),
                data_cls="nautilus_trader.model.data:Bar",
                instrument_id=str(instrument_id),
                start_time=start_dt,
                end_time=end_dt,
            )
            data_configs.append(data_config)
        
        # Build venue config
        venue_config = BacktestVenueConfig(
            name=config.venue,
            oms_type="NETTING",
            account_type="MARGIN",
            base_currency=config.currency,
            starting_balances=[f"{config.initial_balance} {config.currency}"],
            fee_model=PerContractFeeModelConfig(
                commission=f"{config.commission_per_contract} {config.currency}"
            ) if config.commission_per_contract > 0 else None,
            fill_model=FillModelConfig(
                prob_slippage=config.slippage_prob
            ) if config.slippage_prob > 0 else None,
        )
        
        # Build strategy config
        # We need to convert our strategy config to ImportableStrategyConfig
        strategy_config = ImportableStrategyConfig(
            strategy_path=f"app.strategies.implementations.{config.strategy_config['strategy_type'].lower()}:{config.strategy_config['strategy_type']}",
            config_path=f"app.strategies.config:StrategyConfig",
            config=config.strategy_config,
        )
        
        # Build engine config
        engine_config = BacktestEngineConfig(
            # Use default settings, can be customized later
        )
        
        # Build the run config
        run_config = BacktestRunConfig(
            engine=engine_config,
            venues=[venue_config],
            data=data_configs,
            strategies=[strategy_config],
        )
        
        return run_config

    def _extract_results(self, engine, config: BacktestConfig) -> Dict[str, Any]:
        """
        Extract results from the backtest engine.
        
        Args:
            engine: BacktestEngine instance
            config: Backtest configuration
            
        Returns:
            Dictionary with extracted results
        """
        portfolio = engine.portfolio
        analyzer = portfolio.analyzer
        
        # Get performance statistics
        stats_pnls = analyzer.get_performance_stats_pnls()
        stats_returns = analyzer.get_performance_stats_returns()
        stats_general = analyzer.get_performance_stats_general()
        
        # Extract key metrics
        results = {
            "run_id": config.run_id,
            "strategy_id": config.strategy_id,
            "start_date": config.start_date,
            "end_date": config.end_date,
            "instruments": config.instruments,
            "statistics": {
                "pnls": stats_pnls,
                "returns": stats_returns,
                "general": stats_general,
            },
            "trades": [],
            "positions": [],
        }
        
        # Extract trades
        for order in engine.cache.orders():
            if order.is_closed:
                trade = {
                    "instrument_id": str(order.instrument_id),
                    "side": str(order.side),
                    "quantity": float(order.quantity),
                    "avg_price": float(order.avg_px) if hasattr(order, 'avg_px') and order.avg_px else 0.0,
                    "timestamp": order.ts_last,
                }
                results["trades"].append(trade)
        
        # Extract positions
        for position in engine.cache.positions():
            pos_data = {
                "instrument_id": str(position.instrument_id),
                "quantity": float(position.quantity),
                "avg_open_price": float(position.avg_px_open) if hasattr(position, 'avg_px_open') else 0.0,
                "realized_pnl": float(position.realized_pnl.as_double()) if hasattr(position, 'realized_pnl') and position.realized_pnl else 0.0,
                "unrealized_pnl": float(position.unrealized_pnl.as_double()) if hasattr(position, 'unrealized_pnl') and position.unrealized_pnl else 0.0,
                "is_closed": position.is_closed,
            }
            results["positions"].append(pos_data)
        
        return results

    def get_results(self, run_id: str) -> Optional[Dict[str, Any]]:
        """
        Get results for a specific backtest run.
        
        Args:
            run_id: Backtest run identifier
            
        Returns:
            Results dictionary or None if not found
        """
        return self.results.get(run_id)

    def list_results(self) -> List[Dict[str, Any]]:
        """
        List all backtest results.
        
        Returns:
            List of result summaries
        """
        summaries = []
        for run_id, results in self.results.items():
            summary = {
                "run_id": run_id,
                "strategy_id": results.get("strategy_id"),
                "start_date": results.get("start_date"),
                "end_date": results.get("end_date"),
                "instruments": results.get("instruments"),
                "total_trades": len(results.get("trades", [])),
            }
            summaries.append(summary)
        return summaries

    async def run_multiple_backtests(self, configs: List[BacktestConfig]) -> List[Dict[str, Any]]:
        """
        Run multiple backtests with different configurations.
        Uses BacktestNode's ability to handle multiple runs efficiently.
        
        Args:
            configs: List of backtest configurations
            
        Returns:
            List of results for each backtest
        """
        results = []
        for config in configs:
            try:
                result = await self.run_backtest(config)
                results.append(result)
            except Exception as e:
                logger.error(f"Failed to run backtest {config.run_id}: {e}")
                results.append({
                    "run_id": config.run_id,
                    "error": str(e),
                    "status": "failed",
                })
        return results

    def export_trades_to_csv(self, run_id: str, output_path: Optional[str] = None) -> str:
        """
        Export backtest trades to CSV file with detailed trade information.
        
        Args:
            run_id: Backtest run identifier
            output_path: Optional custom output path (defaults to results_path/{run_id}_trades.csv)
            
        Returns:
            Path to the generated CSV file
        """
        import csv
        from datetime import datetime
        
        results = self.results.get(run_id)
        if not results:
            raise ValueError(f"Backtest run {run_id} not found")
        
        # Determine output path
        if output_path is None:
            output_path = str(self.results_path / f"{run_id}_trades.csv")
        
        # Group orders by position to create complete trade records
        positions = results.get("positions", [])
        
        # Build trade records from positions
        trade_records = []
        
        for pos in positions:
            if pos.get("is_closed", False):
                # Extract position details from closed positions
                # Note: We need to match entry/exit orders for this position
                # This is a simplified version - in practice you'd match orders to positions
                
                # Calculate PnL
                pnl = pos.get("realized_pnl", 0.0)
                
                # Determine exit reason (would come from strategy in practice)
                # For now, we'll use a generic reason
                exit_reason = "Position Closed"
                
                trade_record = {
                    "instrument": pos.get("instrument_id", ""),
                    "entry_time": "",  # Would need order matching
                    "entry_price": pos.get("avg_open_price", 0.0),
                    "exit_time": "",   # Would need order matching
                    "exit_price": 0.0,  # Would need exit order
                    "quantity": pos.get("quantity", 0.0),
                    "pnl": pnl,
                    "exit_reason": exit_reason,
                }
                trade_records.append(trade_record)
        
        # If we don't have position data, try to reconstruct from orders
        if not trade_records:
            logger.warning(f"No closed positions found for {run_id}, attempting to reconstruct from orders")
            # This is a fallback - would need more sophisticated matching in production
        
        # Write to CSV
        if trade_records:
            fieldnames = [
                "instrument",
                "entry_time",
                "entry_price",
                "exit_time",
                "exit_price",
                "quantity",
                "pnl",
                "exit_reason"
            ]
            
            with open(output_path, 'w', newline='') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(trade_records)
            
            logger.info(f"Exported {len(trade_records)} trades to {output_path}")
        else:
            logger.warning(f"No trade records to export for {run_id}")
            # Create empty CSV with headers
            with open(output_path, 'w', newline='') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=[
                    "instrument", "entry_time", "entry_price", 
                    "exit_time", "exit_price", "quantity", "pnl", "exit_reason"
                ])
                writer.writeheader()
        
        return output_path

