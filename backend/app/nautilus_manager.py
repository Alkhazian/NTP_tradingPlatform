import asyncio
import logging
import os
from typing import Optional
from nautilus_trader.adapters.interactive_brokers.config import (
    InteractiveBrokersDataClientConfig,
    InteractiveBrokersExecClientConfig,
    InteractiveBrokersInstrumentProviderConfig,
)
from nautilus_trader.adapters.interactive_brokers.common import IBContract
from nautilus_trader.adapters.interactive_brokers.factories import (
    InteractiveBrokersLiveExecClientFactory,
)
from .adapters.custom_ib import CustomInteractiveBrokersLiveDataClientFactory
from .actors.spx_streamer import SpxStreamer, SpxStreamerConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import AccountId, Venue

logger = logging.getLogger(__name__)


class NautilusManager:
    """
    Manages NautilusTrader TradingNode for Interactive Brokers integration.
    Replaces the legacy IBConnector with event-driven architecture.
    """

    def __init__(self, host: str = "ib-gateway", port: int = 4002):
        self.host = host
        self.port = port
        self.node: Optional[TradingNode] = None
        self._connected = False
        self._net_liquidation = "0.0"
        self._open_positions = 0
        self._positions = []
        self._account_id: Optional[str] = None
        self._account_currency = "EUR"
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._buying_power = "0.0"
        self._day_realized_pnl = "0.0"
        # Additional portfolio metrics
        self._margin_used = "0.0"
        self._margin_available = "0.0"
        self._total_unrealized_pnl = "0.0"
        self._total_realized_pnl = "0.0"
        self._net_exposure = "0.0"
        self._leverage = "1.0"
        self._margin_usage_percent = "0.0"
        self._recent_trades = []

    async def start(self):
        """Initialize and start the NautilusTrader TradingNode"""
        try:
            # Get account ID from environment
            account_id = os.getenv("TWS_ACCOUNT")
            if not account_id:
                raise ValueError(
                    "TWS_ACCOUNT environment variable is required for NautilusTrader"
                )

            self._account_id = account_id
            username = os.getenv("TWS_USERID", "")
            password = os.getenv("TWS_PASSWORD", "")
            trading_mode = os.getenv("TRADING_MODE", "paper")

            logger.info(
                f"Initializing NautilusTrader with IB Gateway at {self.host}:{self.port}"
            )

            # Configure Interactive Brokers data client
            ib_data_config = InteractiveBrokersDataClientConfig(
                ibg_host=self.host,
                ibg_port=self.port,
                ibg_client_id=101,
            )

            # Configure Interactive Brokers execution client
            ib_exec_config = InteractiveBrokersExecClientConfig(
                ibg_host=self.host,
                ibg_port=self.port,
                ibg_client_id=101,
                account_id=account_id,
            )

            # Configure instrument provider
            ib_instrument_config = InteractiveBrokersInstrumentProviderConfig(
                load_all=False,
                load_contracts=[
                    IBContract(secType="IND", symbol="SPX", exchange="CBOE", currency="USD")
                ],
            )

            # Create TradingNode configuration
            config = TradingNodeConfig(
                data_clients={
                    "InteractiveBrokers": ib_data_config,
                },
                exec_clients={
                    "InteractiveBrokers": ib_exec_config,
                },
                timeout_connection=90.0,
                timeout_reconciliation=10.0,
                timeout_portfolio=10.0,
                timeout_disconnection=10.0,
            )

            # Create and build the trading node
            self.node = TradingNode(config=config)
            self.node.add_data_client_factory(
                "InteractiveBrokers", CustomInteractiveBrokersLiveDataClientFactory
            )
            self.node.add_exec_client_factory(
                "InteractiveBrokers", InteractiveBrokersLiveExecClientFactory
            )
            self.node.build()

            # Initialize Strategy Manager
            from .strategies.manager import StrategyManager
            self.strategy_manager = StrategyManager(self.node)

            # Start the node in the background
            asyncio.create_task(self.node.run_async())

            # Initialize strategies (restore state)
            # We wait a brief moment for the node to fully start engines
            # In a real app we might listen for a "started" event or use a loop
            # But here we just schedule initialization
            async def init_strategies():
                await asyncio.sleep(10) # Allow node loops to spin up and sync instruments
                await self.strategy_manager.initialize()
            
            asyncio.create_task(init_strategies())

            self._connected = True
            logger.info("NautilusTrader TradingNode started in background")

        except Exception as e:
            logger.error(f"Failed to start NautilusTrader: {e}")
            self._connected = False
            raise

    async def _update_account_state(self):
        """Fetch and update account state from NautilusTrader"""
        try:
            if not self.node or not self._connected:
                return

            # Get the portfolio from the node
            portfolio = self.node.portfolio

            # Get account state
            target_account_id = f"InteractiveBrokers-{self._account_id}"
            
            account = None
            # Use node cache to find accounts
            for acc in self.node.cache.accounts():
                if str(acc.id) == target_account_id:
                    account = acc
                    break

            if account:
                # Get balances - handle both method and property
                try:
                    balances_raw = account.balances() if callable(getattr(account, "balances", None)) else account.balances
                except Exception:
                    balances_raw = getattr(account, "balances", [])

                if balances_raw:
                    # Convert to list if it's a mapping/dict
                    if hasattr(balances_raw, "values"):
                        balances = list(balances_raw.values())
                    else:
                        balances = list(balances_raw)
                    
                    if balances:
                        # Use the first available balance
                        balance = balances[0]
                        self._account_currency = str(balance.total.currency.code)
                        self._net_liquidation = f"{balance.total.as_double():.2f} {self._account_currency}"
                        
                        # Extract buying power (free balance)
                        try:
                            if hasattr(balance, "free"):
                                self._buying_power = f"{balance.free.as_double():.2f} {balance.free.currency.code}"
                            else:
                                # Fallback if free not directly available (unlikely for Balance object)
                                self._buying_power = f"{balance.total.as_double():.2f} {balance.total.currency.code}"
                        except Exception:
                            self._buying_power = "0.00 EUR"
                            
                        logger.debug(f"Account updated - Net Liquidation: {self._net_liquidation}, Buying Power: {self._buying_power}")
                    else:
                        logger.info(f"No individual balances found in account {target_account_id}")
                else:
                    logger.info(f"No balances found for account {target_account_id}")

                # Update open positions details
                positions_data = []
                try:
                    for p in self.node.cache.positions():
                        if not p.is_closed:
                            # Safely extract PnL if available
                            pnl = 0.0
                            try:
                                if p.unrealized_pnl:
                                    pnl = float(p.unrealized_pnl.as_double())
                            except Exception:
                                pass

                            # Safely extract price
                            avg_price = 0.0
                            try:
                                if p.avg_px_open is not None:
                                    if hasattr(p.avg_px_open, "as_double"):
                                        avg_price = float(p.avg_px_open.as_double())
                                    else:
                                        avg_price = float(p.avg_px_open)
                            except Exception:
                                pass

                            positions_data.append({
                                "symbol": str(p.instrument_id),
                                "quantity": float(p.quantity),
                                "avg_price": avg_price,
                                "unrealized_pnl": pnl
                            })
                except Exception as e:
                    logger.error(f"Error processing positions: {e}")
                
                self._positions = positions_data
                self._open_positions = len(self._positions)

                # Calculate daily realized P&L from closed positions today
                try:
                    from datetime import datetime, timezone
                    
                    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                    daily_realized = 0.0
                    
                    # Get all positions (including closed ones)
                    for p in self.node.cache.positions():
                        if p.is_closed:
                            # Check if position was closed today
                            if hasattr(p, 'ts_closed') and p.ts_closed:
                                # Convert nanoseconds timestamp to datetime
                                closed_time = datetime.fromtimestamp(p.ts_closed / 1_000_000_000, tz=timezone.utc)
                                
                                if closed_time >= today_start:
                                    # Add realized PnL from this position
                                    if hasattr(p, 'realized_pnl') and p.realized_pnl:
                                        try:
                                            pnl_value = float(p.realized_pnl.as_double())
                                            daily_realized += pnl_value
                                        except Exception:
                                            pass
                    
                    self._day_realized_pnl = f"{daily_realized:.2f} {self._account_currency}"
                    logger.debug(f"Daily realized P&L: {self._day_realized_pnl}")
                    
                except Exception as e:
                    logger.error(f"Error calculating daily realized P&L: {e}", exc_info=True)
                    self._day_realized_pnl = "0.00 USD"

                # Extract additional portfolio metrics
                try:
                    # Get margin information
                    if hasattr(account, 'margins_init') and callable(account.margins_init):
                        margins_init = account.margins_init()
                        if margins_init:
                            margin_list = list(margins_init.values()) if hasattr(margins_init, 'values') else list(margins_init)
                            if margin_list:
                                margin_init = margin_list[0]
                                self._margin_used = f"{margin_init.as_double():.2f} {margin_init.currency.code}"
                    
                    # Calculate margin available (buying power - margin used)
                    try:
                        buying_power_val = float(self._buying_power.split()[0])
                        margin_used_val = float(self._margin_used.split()[0]) if self._margin_used != "0.0" else 0.0
                        margin_available = buying_power_val - margin_used_val
                        self._margin_available = f"{margin_available:.2f} {self._account_currency}"
                        
                        # Calculate margin usage percentage
                        net_liq_val = float(self._net_liquidation.split()[0])
                        if net_liq_val > 0:
                            margin_pct = (margin_used_val / net_liq_val) * 100
                            self._margin_usage_percent = f"{margin_pct:.1f}"
                    except Exception:
                        pass

                    # Get portfolio-level metrics
                    portfolio = self.node.portfolio
                    
                    # Total unrealized P&L
                    # Total Unrealized P&L (from node cache positions)
                    self._total_unrealized_pnl = f"0.00 {self._account_currency}"
                    try:
                        positions = list(self.node.cache.positions())
                        if positions:
                            unrealized = sum(p.unrealized_pnl.as_double() for p in positions if hasattr(p, 'unrealized_pnl') and p.unrealized_pnl)
                            self._total_unrealized_pnl = f"{unrealized:.2f} {self._account_currency}"
                    except Exception:
                        pass

                    # Total realized PnL (from node cache positions)
                    self._total_realized_pnl = f"0.00 {self._account_currency}"
                    try:
                        pnl_list = [p.realized_pnl for p in self.node.cache.positions()]
                        if pnl_list:
                            total_realized = sum(p.as_double() for p in pnl_list if p is not None)
                            self._total_realized_pnl = f"{total_realized:.2f} {self._account_currency}"
                    except Exception:
                        pass
                    
                    # Net exposure
                    try:
                        if hasattr(portfolio, 'net_exposures'):
                            from nautilus_trader.core.enums import Venue
                            venue = Venue("InteractiveBrokers")
                            # Check if account is actually registered with portfolio before calling net_exposures
                            # to avoid "[ERROR] TRADER-001.Portfolio: Cannot calculate net exposures"
                            if hasattr(portfolio, "_account_data") and venue in portfolio._account_data:
                                net_exposures = portfolio.net_exposures(venue)
                                if net_exposures:
                                    if hasattr(net_exposures, 'as_double'):
                                        self._net_exposure = f"{abs(net_exposures.as_double()):.2f} {self._account_currency}"
                                    elif isinstance(net_exposures, dict) or hasattr(net_exposures, 'values'):
                                        exp_list = list(net_exposures.values()) if hasattr(net_exposures, 'values') else list(net_exposures)
                                        if exp_list:
                                            total_exposure = sum(e.as_double() for e in exp_list if e is not None)
                                            self._net_exposure = f"{abs(total_exposure):.2f} {self._account_currency}"
                    except Exception:
                        pass
                    
                    # Leverage
                    try:
                        if hasattr(account, 'leverages'):
                            leverages = account.leverages()
                            if leverages:
                                lev_list = list(leverages.values()) if hasattr(leverages, 'values') else list(leverages)
                                if lev_list:
                                    self._leverage = f"{lev_list[0]:.2f}"
                    except Exception:
                        pass
                    
                    logger.debug(f"Portfolio metrics - Margin Used: {self._margin_used}, Unrealized P&L: {self._total_unrealized_pnl}, Net Exposure: {self._net_exposure}")
                    
                except Exception as e:
                    logger.error(f"Error extracting portfolio metrics: {e}", exc_info=True)
            else:
                logger.warning(f"Account {target_account_id} not found in cache")

        except Exception as e:
            logger.error(f"Error updating account state: {e}", exc_info=True)

    async def _get_recent_trades(self, hours: int = 24):
        """Fetch recent trade activity from the last N hours"""
        try:
            if not self.node or not self._connected:
                return []

            from datetime import datetime, timezone, timedelta
            
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)
            cutoff_ns = int(cutoff_time.timestamp() * 1_000_000_000)
            
            
            trades = []
            
            # Get all orders from cache
            for order in self.node.cache.orders():
                # Only include filled orders
                if not hasattr(order, 'is_closed') or not order.is_closed:
                    continue
                
                # Check if order was filled within the time window
                if hasattr(order, 'ts_last') and order.ts_last:
                    if order.ts_last < cutoff_ns:
                        continue
                    
                    # Extract order details
                    try:
                        # Determine trade type
                        trade_type = "buy" if str(order.side) == "OrderSide.BUY" else "sell"
                        
                        # Get symbol
                        symbol = str(order.instrument_id).split('.')[0] if hasattr(order, 'instrument_id') else "UNKNOWN"
                        
                        # Get quantity
                        quantity = float(order.quantity) if hasattr(order, 'quantity') else 0.0
                        
                        # Get average fill price
                        avg_price = 0.0
                        if hasattr(order, 'avg_px') and order.avg_px:
                            avg_price = float(order.avg_px) if not hasattr(order.avg_px, 'as_double') else float(order.avg_px.as_double())
                        
                        # Calculate time ago
                        filled_time = datetime.fromtimestamp(order.ts_last / 1_000_000_000, tz=timezone.utc)
                        time_diff = datetime.now(timezone.utc) - filled_time
                        
                        if time_diff.days > 0:
                            time_ago = f"{time_diff.days} day{'s' if time_diff.days > 1 else ''} ago"
                        elif time_diff.seconds >= 3600:
                            hours_ago = time_diff.seconds // 3600
                            time_ago = f"{hours_ago} hour{'s' if hours_ago > 1 else ''} ago"
                        elif time_diff.seconds >= 60:
                            minutes_ago = time_diff.seconds // 60
                            time_ago = f"{minutes_ago} minute{'s' if minutes_ago > 1 else ''} ago"
                        else:
                            time_ago = "Just now"
                        
                        trades.append({
                            "type": trade_type,
                            "symbol": symbol,
                            "quantity": quantity,
                            "price": avg_price,
                            "time": time_ago,
                            "timestamp": order.ts_last
                        })
                        
                    except Exception as e:
                        logger.error(f"Error processing order {order}: {e}")
                        continue
            
            # Sort by timestamp (most recent first)
            trades.sort(key=lambda x: x['timestamp'], reverse=True)
            
            # Keep timestamp for frontend filtering
            # for trade in trades:
            #     trade.pop('timestamp', None)
            
            logger.debug(f"Found {len(trades)} recent trades in the last {hours} hours")
            return trades  # Return all trades for frontend filtering
            
        except Exception as e:
            logger.error(f"Error fetching recent trades: {e}", exc_info=True)
            return []

    async def stop(self):


        """Stop the NautilusTrader TradingNode"""
        if self.node:
            logger.info("Stopping NautilusTrader TradingNode")
            await self.node.stop_async()
            self._connected = False
            logger.info("NautilusTrader TradingNode stopped")

    async def start_spx_stream(self):
        """Start the SPX Streaming Data Actor"""
        if not self.strategy_manager:
            raise RuntimeError("Strategy Manager not initialized")
        
        # Ensure SPX is loaded in cache
        # Note: We configured the InstrumentProvider to load SPX.CBOE on startup.
        # Direct access to load it here is difficult without exposing internal clients.
        # We assume it is loaded or will be loaded.
            # We continue, maybe it's already there or will fail in strategy
        
        # Check if already exists
        strategies = self.strategy_manager.get_all_strategies_status()
        spx_strat_exists = any(s['id'] == 'spx-streamer-01' for s in strategies)
        
        # If not, create it
        if not spx_strat_exists:
            config = SpxStreamerConfig(
                id="spx-streamer-01",
                name="SPX Streamer",
                strategy_type="SpxStreamer",
                instrument_id="^SPX.CBOE", 
                redis_url="redis://redis:6379/0"
            )

            # Ensure the class is registered (hot-fix if registry wasn't reloaded)
            if "SpxStreamer" not in self.strategy_manager._strategy_classes:
                self.strategy_manager._load_registry()

            # StrategyManager.create_strategy takes StrategyConfig
            await self.strategy_manager.create_strategy(config)
            
        await self.strategy_manager.start_strategy('spx-streamer-01')
        return 'spx-streamer-01'

    async def stop_spx_stream(self):
        """Stop the SPX Streaming Data Actor"""
        if not self.strategy_manager:
             raise RuntimeError("Strategy Manager not initialized")
        
        await self.strategy_manager.stop_strategy('spx-streamer-01')
        return 'spx-streamer-01'

    def get_status(self) -> dict:
        """
        Get current connection and account status.
        Maintains compatibility with legacy IBConnector interface.
        """
        return {
            "type": "system_status",
            "connected": self._connected,
            "nautilus_active": self.node is not None,
            "net_liquidation": self._net_liquidation,
            "open_positions": self._open_positions,
            "positions": self._positions,
            "account_id": self._account_id,
            "buying_power": self._buying_power,
            "account_currency": self._account_currency,
            "day_realized_pnl": self._day_realized_pnl,
            # Additional portfolio metrics
            "margin_used": self._margin_used,
            "margin_available": self._margin_available,
            "margin_usage_percent": self._margin_usage_percent,
            "total_unrealized_pnl": self._total_unrealized_pnl,
            "total_realized_pnl": self._total_realized_pnl,
            "net_exposure": self._net_exposure,
            "leverage": self._leverage,
            "recent_trades": self._recent_trades,
            "strategies": self.strategy_manager.get_all_strategies_status() if self.strategy_manager else [],
        }

    async def update_status(self):
        """Update account state - call periodically for fresh data"""
        await self._update_account_state()
        self._recent_trades = await self._get_recent_trades(hours=24)
