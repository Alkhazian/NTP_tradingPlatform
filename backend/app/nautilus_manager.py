import asyncio
import logging
import os
from typing import Optional, List
from nautilus_trader.adapters.interactive_brokers.config import (
    InteractiveBrokersDataClientConfig,
    InteractiveBrokersExecClientConfig,
    InteractiveBrokersInstrumentProviderConfig,
    SymbologyMethod,
)
from nautilus_trader.adapters.interactive_brokers.common import IBContract
from nautilus_trader.adapters.interactive_brokers.factories import (
    InteractiveBrokersLiveDataClientFactory,
    InteractiveBrokersLiveExecClientFactory,
)
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import AccountId, Venue, InstrumentId

# Import our strategy
from .strategies.implementations.spx_0dte_straddle import (
    Spx0DteStraddleStrategy,
    Spx0DteStraddleConfig,
)

logger = logging.getLogger(__name__)


class NautilusManager:
    """
    Manages NautilusTrader TradingNode for Interactive Brokers integration.
    Replaces the legacy IBConnector with event-driven architecture.
    
    Now includes:
    - SPX index instrument loading via IBContract
    - Strategy management (start/stop)
    - Strategy logging for UI display
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
        self._account_currency = "USD"
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
        
        # Strategy management
        self._spx_strategy: Optional[Spx0DteStraddleStrategy] = None
        self._strategy_active = False
        self._strategy_logs: List[str] = []
        self._max_strategy_logs = 200

    def _log_strategy_event(self, message: str) -> None:
        """Add a log entry for strategy management events."""
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        entry = f"[{timestamp}] [MANAGER] {message}"
        self._strategy_logs.append(entry)
        if len(self._strategy_logs) > self._max_strategy_logs:
            self._strategy_logs = self._strategy_logs[-self._max_strategy_logs:]
        logger.info(f"[StrategyManager] {message}")

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

            # Configure instrument provider with SPX index
            # Using IBContract for proper index specification
            # Note: IB uses ^ prefix for indices in simplified symbology
            spx_contract = IBContract(
                secType="IND",
                symbol="SPX",
                exchange="CBOE",
                currency="USD",
            )
            
            ib_instrument_config = InteractiveBrokersInstrumentProviderConfig(
                symbology_method=SymbologyMethod.IB_SIMPLIFIED,
                load_all=False,
                # Load SPX using simplified symbology (^ prefix for indices)
                load_ids=frozenset([
                    "^SPX.CBOE",  # Index with ^ prefix (IB simplified format)
                ]),
                # Also load via contract for reliability
                load_contracts=frozenset([
                    spx_contract,
                ]),
            )

            # Configure Interactive Brokers data client
            ib_data_config = InteractiveBrokersDataClientConfig(
                ibg_host=self.host,
                ibg_port=self.port,
                ibg_client_id=101,
                instrument_provider=ib_instrument_config,
                # Handle index data quirks
                ignore_quote_tick_size_updates=True,  # Reduce noise for indices
            )

            # Configure Interactive Brokers execution client
            ib_exec_config = InteractiveBrokersExecClientConfig(
                ibg_host=self.host,
                ibg_port=self.port,
                ibg_client_id=101,
                account_id=account_id,
                instrument_provider=ib_instrument_config,
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
                "InteractiveBrokers", InteractiveBrokersLiveDataClientFactory
            )
            self.node.add_exec_client_factory(
                "InteractiveBrokers", InteractiveBrokersLiveExecClientFactory
            )
            self.node.build()
            
            # Log instrument loading
            self._log_strategy_event("NautilusTrader node built")
            self._log_strategy_event(f"Configured to load SPX.CBOE (^SPX.CBOE)")
            
            # Start the node in the background
            asyncio.create_task(self.node.run_async())

            self._connected = True
            logger.info("NautilusTrader TradingNode started in background")
            self._log_strategy_event("TradingNode started successfully")

        except Exception as e:
            logger.error(f"Failed to start NautilusTrader: {e}")
            self._log_strategy_event(f"ERROR: Failed to start - {e}")
            self._connected = False
            raise

    async def start_spx_strategy(
        self,
        strike_offset: int = 0,
        days_to_expiry: int = 0,
        refresh_interval_seconds: int = 60
    ) -> dict:
        """
        Start the SPX 0DTE Straddle Strategy.
        
        Returns:
            Dict with status information
        """
        if not self.node or not self._connected:
            self._log_strategy_event("ERROR: Cannot start strategy - node not connected")
            return {"success": False, "error": "TradingNode not connected"}
        
        if self._strategy_active and self._spx_strategy:
            self._log_strategy_event("Strategy already running")
            return {"success": False, "error": "Strategy already running"}
        
        try:
            self._log_strategy_event("Starting SPX 0DTE Straddle Strategy...")
            
            # Create strategy config
            # Try different instrument ID formats that IB might use
            instrument_ids_to_try = [
                "SPX.CBOE",      # Standard format
                "^SPX.CBOE",     # Index prefix format
                "SPX.XCBO",      # MIC code format
            ]
            
            # Check which instrument ID is available in cache
            available_instruments = list(self.node.cache.instrument_ids())
            self._log_strategy_event(f"Available instruments in cache: {len(available_instruments)}")
            
            # Find the correct SPX instrument ID
            spx_instrument_id = None
            for possible_id in instrument_ids_to_try:
                if InstrumentId.from_str(possible_id) in available_instruments:
                    spx_instrument_id = possible_id
                    self._log_strategy_event(f"Found SPX instrument: {spx_instrument_id}")
                    break
            
            # If not found, also search by symbol
            if not spx_instrument_id:
                for inst_id in available_instruments:
                    if "SPX" in str(inst_id).upper():
                        spx_instrument_id = str(inst_id)
                        self._log_strategy_event(f"Found SPX instrument by search: {spx_instrument_id}")
                        break
            
            # Default to standard format if still not found
            if not spx_instrument_id:
                spx_instrument_id = "SPX.CBOE"
                self._log_strategy_event(f"Using default instrument ID: {spx_instrument_id}")
            
            # Generate unique strategy ID with timestamp to avoid conflicts
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            unique_order_id_tag = f"SPX0DTE_{timestamp}"
            
            config = Spx0DteStraddleConfig(
                instrument_id=spx_instrument_id,
                use_bars=True,  # Use bars for stability with index data
                bar_interval_seconds=5,
                order_id_tag=unique_order_id_tag,
                strike_offset=strike_offset,
                days_to_expiry=days_to_expiry,
                refresh_interval_seconds=refresh_interval_seconds,
            )
            
            # Create strategy instance
            self._spx_strategy = Spx0DteStraddleStrategy(config=config)
            self._log_strategy_event(f"Strategy instance created with config: {config}")
            
            # Add strategy to trader
            self.node.trader.add_strategy(self._spx_strategy)
            self._log_strategy_event("Strategy added to trader")
            
            # Start the strategy
            self._spx_strategy.start()
            self._strategy_active = True
            self._log_strategy_event("Strategy started successfully!")
            
            return {
                "success": True,
                "message": "Strategy started",
                "instrument_id": spx_instrument_id
            }
            
        except Exception as e:
            error_msg = f"Failed to start strategy: {str(e)}"
            self._log_strategy_event(f"ERROR: {error_msg}")
            logger.exception("Strategy start failed")
            return {"success": False, "error": error_msg}

    async def stop_spx_strategy(self) -> dict:
        """
        Stop the SPX 0DTE Straddle Strategy.
        
        Returns:
            Dict with status information
        """
        if not self._spx_strategy or not self._strategy_active:
            self._log_strategy_event("Strategy not running")
            return {"success": False, "error": "Strategy not running"}
        
        try:
            self._log_strategy_event("Stopping SPX 0DTE Straddle Strategy...")
            
            # Stop the strategy
            self._spx_strategy.stop()
            
            # Clear strategy reference (new instance will be created on next start)
            self._spx_strategy = None
            self._strategy_active = False
            self._log_strategy_event("Strategy stopped successfully!")
            
            return {"success": True, "message": "Strategy stopped"}
            
        except Exception as e:
            error_msg = f"Failed to stop strategy: {str(e)}"
            self._log_strategy_event(f"ERROR: {error_msg}")
            return {"success": False, "error": error_msg}



    def get_strategy_status(self) -> dict:
        """Get current strategy status for UI display."""
        strategy_logs = self._strategy_logs.copy()
        
        # Add strategy's internal logs if available
        if self._spx_strategy:
            strategy_internal_logs = self._spx_strategy.get_strategy_logs()
            strategy_logs.extend(strategy_internal_logs)
        
        # Sort by timestamp (logs start with [HH:MM:SS.mmm])
        strategy_logs.sort()
        
        return {
            "name": "SPX 0DTE Opening Straddle",
            "is_active": self._strategy_active,
            "current_price": self._spx_strategy.get_current_price() if self._spx_strategy else None,
            "status": self._spx_strategy.get_status() if self._spx_strategy else {},
            "logs": strategy_logs[-100:],  # Last 100 log entries
        }

    async def inject_mock_price(self, price: float) -> dict:
        """
        Inject a mock price update into the strategy for testing.
        
        This allows testing the strategy and UI while the market is closed.
        It directly updates the strategy's internal state without going
        through the normal data flow.
        
        Args:
            price: The mock price value to inject
            
        Returns:
            Dict with status information
        """
        if not self._spx_strategy or not self._strategy_active:
            self._log_strategy_event("Cannot inject mock data - strategy not running")
            return {"success": False, "error": "Strategy not running"}
        
        try:
            from datetime import datetime, timezone
            
            # Directly update strategy state
            # Note: Indices don't have bid/ask, only Last Price
            self._spx_strategy._current_price = price
            self._spx_strategy._last_update_time = datetime.now(timezone.utc)
            self._spx_strategy._quote_tick_count += 1
            
            # Log the mock data
            self._spx_strategy._log_strategy(
                f"MOCK DATA: Last Price={price:.2f}",
                "DEBUG"
            )
            self._log_strategy_event(f"Injected mock price: {price:.2f}")
            
            return {
                "success": True,
                "message": f"Mock price {price:.2f} injected",
                "current_price": price
            }
            
        except Exception as e:
            error_msg = f"Failed to inject mock price: {str(e)}"
            self._log_strategy_event(f"ERROR: {error_msg}")
            return {"success": False, "error": error_msg}



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
                            self._buying_power = "0.00 USD"
                            
                        logger.info(f"Account updated - Net Liquidation: {self._net_liquidation}, Buying Power: {self._buying_power}")
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
                    logger.info(f"Daily realized P&L: {self._day_realized_pnl}")
                    
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
                    if hasattr(portfolio, 'unrealized_pnls'):
                        unrealized_pnls = portfolio.unrealized_pnls(None)  # None for all venues
                        if unrealized_pnls:
                            pnl_list = list(unrealized_pnls.values()) if hasattr(unrealized_pnls, 'values') else list(unrealized_pnls)
                            if pnl_list:
                                total_unrealized = sum(p.as_double() for p in pnl_list if p is not None)
                                self._total_unrealized_pnl = f"{total_unrealized:.2f} {self._account_currency}"
                    
                    # Total realized P&L (all time)
                    if hasattr(portfolio, 'realized_pnls'):
                        realized_pnls = portfolio.realized_pnls(None)
                        if realized_pnls:
                            pnl_list = list(realized_pnls.values()) if hasattr(realized_pnls, 'values') else list(realized_pnls)
                            if pnl_list:
                                total_realized = sum(p.as_double() for p in pnl_list if p is not None)
                                self._total_realized_pnl = f"{total_realized:.2f} {self._account_currency}"
                    
                    # Net exposure
                    if hasattr(portfolio, 'net_exposures'):
                        net_exposures = portfolio.net_exposures(None)
                        if net_exposures:
                            exp_list = list(net_exposures.values()) if hasattr(net_exposures, 'values') else list(net_exposures)
                            if exp_list:
                                total_exposure = sum(e.as_double() for e in exp_list if e is not None)
                                self._net_exposure = f"{abs(total_exposure):.2f} {self._account_currency}"
                    
                    # Leverage
                    if hasattr(account, 'leverages'):
                        leverages = account.leverages()
                        if leverages:
                            lev_list = list(leverages.values()) if hasattr(leverages, 'values') else list(leverages)
                            if lev_list:
                                # Get the first leverage value
                                self._leverage = f"{lev_list[0]:.2f}"
                    
                    logger.info(f"Portfolio metrics - Margin Used: {self._margin_used}, Unrealized P&L: {self._total_unrealized_pnl}, Net Exposure: {self._net_exposure}")
                    
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
            
            logger.info(f"Found {len(trades)} recent trades in the last {hours} hours")
            return trades  # Return all trades for frontend filtering
            
        except Exception as e:
            logger.error(f"Error fetching recent trades: {e}", exc_info=True)
            return []

    async def stop(self):
        """Stop the NautilusTrader TradingNode"""
        # First stop the strategy if running
        if self._strategy_active and self._spx_strategy:
            try:
                self._spx_strategy.stop()
                self._strategy_active = False
                self._log_strategy_event("Strategy stopped during shutdown")
            except Exception as e:
                logger.error(f"Error stopping strategy: {e}")
        
        if self.node:
            logger.info("Stopping NautilusTrader TradingNode")
            await self.node.stop_async()
            self._connected = False
            logger.info("NautilusTrader TradingNode stopped")

    def get_status(self) -> dict:
        """
        Get current connection and account status.
        Maintains compatibility with legacy IBConnector interface.
        Now includes strategy status for UI display.
        """
        # Get strategy status
        strategy_status = self.get_strategy_status()
        
        return {
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
            # Strategy section
            "strategy": strategy_status,
        }

    async def update_status(self):
        """Update account state - call periodically for fresh data"""
        await self._update_account_state()
        self._recent_trades = await self._get_recent_trades(hours=24)

