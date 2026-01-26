import asyncio
import logging
import os
from typing import Optional, Dict, Any
from nautilus_trader.adapters.interactive_brokers.config import (
    InteractiveBrokersDataClientConfig,
    InteractiveBrokersExecClientConfig,
    InteractiveBrokersInstrumentProviderConfig,
    IBMarketDataTypeEnum,
    SymbologyMethod,
)
from nautilus_trader.adapters.interactive_brokers.common import IBContract
from nautilus_trader.adapters.interactive_brokers.factories import (
    InteractiveBrokersLiveExecClientFactory,
)
from .adapters.custom_ib import CustomInteractiveBrokersLiveDataClientFactory
from .actors.spx_streamer import SpxStreamer, SpxStreamerConfig
from nautilus_trader.config import (
    TradingNodeConfig,
    RoutingConfig,
    CacheConfig,
    MessageBusConfig,
    DatabaseConfig
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import AccountId, Venue, Symbol
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.data import BarType
from .services.trade_recorder import TradeRecorder

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
        self.strategy_manager = None
        self.trade_recorder = None # Initialize lazily or here

    @property
    def nautilus_account_id(self) -> Optional[AccountId]:
        """Get the full AccountId used by Nautilus."""
        if not self._account_id:
            return None
        return AccountId(f"InteractiveBrokers-{self._account_id}")

    async def start(self):
        """Initialize and start the NautilusTrader TradingNode"""
        try:
            # Initialize TradeRecorder
            self.trade_recorder = TradeRecorder() # Sync init for now, lightweight
            
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

            # Stability: Wait for IB Gateway to be fully ready and settled
            import socket
            logger.info(f"Waiting for IB Gateway at {self.host}:{self.port} to settle (60s)...")
            gateway_ready = False
            for _ in range(30):
                try:
                    with socket.create_connection((self.host, self.port), timeout=1):
                        gateway_ready = True
                        break
                except Exception:
                    await asyncio.sleep(2)
            
            if gateway_ready:
                await asyncio.sleep(20) # Give IBC time to handle dialogs/login
                logger.info("Gateway ready and settled.")
            else:
                logger.warning("Gateway port not reachable, proceeding anyway...")

            # Configure instrument provider with detailed contracts
            ib_instrument_config = InteractiveBrokersInstrumentProviderConfig(
                load_all=False,
                load_contracts=(
                    IBContract(
                        secType="IND", 
                        symbol="SPX", 
                        exchange="CBOE", 
                        currency="USD",
                    ),
                    IBContract(
                        secType="FUT", 
                        symbol="MES", 
                        exchange="CME", 
                        currency="USD",
                        localSymbol="MESH6",
                        lastTradeDateOrContractMonth="202603",
                    ),

                ),
                symbology_method=SymbologyMethod.IB_SIMPLIFIED,
                symbol_to_mic_venue={  
                    "SPX": "CBOE",  "MES": "CME"# Force SPX options to use CBOE  
                },  
                convert_exchange_to_mic_venue=True,
                #filter_sec_types=frozenset({"STK", "FUT", "OPT", "IND"}),
            )

            # Configure Interactive Brokers data client
            ib_data_config = InteractiveBrokersDataClientConfig(
                ibg_host=self.host,
                ibg_port=self.port,
                ibg_client_id=101,
                use_regular_trading_hours=False,
                market_data_type=IBMarketDataTypeEnum.REALTIME, 
                #market_data_type=IBMarketDataTypeEnum.DELAYED_FROZEN, 

                instrument_provider=ib_instrument_config,
            )

            # Configure Interactive Brokers execution client
            ib_exec_config = InteractiveBrokersExecClientConfig(
                ibg_host=self.host,
                ibg_port=self.port,
                ibg_client_id=101,
                account_id=account_id,
                instrument_provider=ib_instrument_config,
                routing=RoutingConfig(default=True),
            )


            # Redis Configuration
            redis_config = DatabaseConfig(
                type="redis",
                host=os.getenv("REDIS_HOST", "redis"),
                port=int(os.getenv("REDIS_PORT", "6379")),
            )

            config = TradingNodeConfig(
                # Enable Object Cache (Orders, Positions)
                cache=CacheConfig(
                    database=redis_config,
                    timestamps_as_iso8601=True,
                ),
                # Enable Message Bus (UI Streaming)
                message_bus=MessageBusConfig(
                    database=redis_config,
                    stream_per_topic=True,
                ),
                data_clients={
                    "CME": ib_data_config,
                    "CBOE": ib_data_config,
                    "SMART": ib_data_config,
                    "InteractiveBrokers": ib_data_config,
                },
                exec_clients={
                    "InteractiveBrokers": ib_exec_config,
                },
                timeout_connection=90.0,
                timeout_reconciliation=60.0,
                timeout_portfolio=60.0,
                timeout_disconnection=10.0,
            )

            # Create and build the trading node
            self.node = TradingNode(config=config)
            
            # Add custom factories for all venues to ensure consistent behavior
            for venue_name in ["CME", "CBOE", "SMART", "InteractiveBrokers"]:
                self.node.add_data_client_factory(
                    venue_name, CustomInteractiveBrokersLiveDataClientFactory
                )
            
            self.node.add_exec_client_factory(
                "InteractiveBrokers", InteractiveBrokersLiveExecClientFactory
            )
            self.node.build()

            # Initialize Strategy Manager
            from .strategies.manager import StrategyManager
            self.strategy_manager = StrategyManager(self.node, integration_manager=self)

            # Initialize strategies (restore state and register with trader)
            # This must happen BEFORE node.run_async() to avoid "Cannot add a strategy to a running trader"
            await self.strategy_manager.initialize()

            # Launch the node startup sequence in the background
            asyncio.create_task(self._run_node_and_start_strategies())

            self._connected = True
            logger.info("NautilusTrader initialization complete, node starting in background")

        except Exception as e:
            logger.error(f"Failed to start NautilusTrader: {e}")
            self._connected = False
            raise

    async def _run_node_and_start_strategies(self):
        """Run the trading node and start enabled strategies once ready"""
        try:
            # Start the node (runs indefinitely in background)
            asyncio.create_task(self.node.run_async())
            
            # Wait for node to be running with timeout
            # TradingNode has 90s timeout for engine connections + 60s for portfolio init
            # So we wait up to 120s total
            logger.info("Waiting for node to be running...")
            max_wait = 120  # seconds
            check_interval = 2.0  # check every 2 seconds
            elapsed = 0
            
            while not self.node.is_running() and elapsed < max_wait:
                await asyncio.sleep(check_interval)
                elapsed += check_interval
                if elapsed % 10 == 0:  # Log every 10 seconds
                    logger.info(f"Still waiting for node... ({elapsed}s elapsed, node.is_running={self.node.is_running()}, trader.is_running={self.node.trader.is_running})")
            
            logger.info(f"Node state after wait: node.is_running={self.node.is_running()}, trader.is_running={self.node.trader.is_running}, elapsed={elapsed:.1f}s")
            
            if self.node.is_running():
                logger.info(f"Node is running (waited {elapsed:.1f}s). Requesting SPX option chains...")
                
#                 # Request SPX option chains from IBKR
#                 try:
#                     self.node.trader.request_instruments(
#                         venue=Venue("CBOE"),
#                         instrument_id=Symbol("SPX")
#                     )
#                     logger.info("Sent request for SPX option chain to IBKR")
#                 except Exception as e:
#                     logger.error(f"Error requesting option chains: {e}")
                
                # Give time for option contracts to load before starting strategies
                await asyncio.sleep(5)
                
                # Start strategies that are marked as enabled
                for strategy_id, strategy in self.strategy_manager.strategies.items():
                    try:
                        logger.info(f"Checking strategy {strategy_id}: enabled={strategy.strategy_config.enabled}, is_running={strategy.is_running}")
                        if strategy.strategy_config.enabled and not strategy.is_running:
                                logger.info(f"Auto-starting enabled strategy: {strategy_id}")
                                await self.strategy_manager.start_strategy(strategy_id)
                    except Exception as e:
                        logger.error(f"Failed to auto-start strategy {strategy_id}: {e}", exc_info=True)
            else:
                logger.warning(f"Node not running after {max_wait}s timeout (node.is_running={self.node.is_running}, trader.is_running={self.node.trader.is_running}), strategies will need manual start")
            
        except Exception as e:
            logger.error(f"Error in background node startup task: {e}", exc_info=True)
            self._connected = False

    async def _update_account_state(self):
        """Fetch and update account state from NautilusTrader"""
        try:
            if not self.node or not self._connected:
                return

            # Get the portfolio from the node
            portfolio = self.node.portfolio

            # Use node cache to find accounts
            target_account_id = f"InteractiveBrokers-{self._account_id}"
            account = None
            
            all_accounts = list(self.node.cache.accounts())
            if not all_accounts:
                # If cache is literally empty, we can't do much yet
                return

            for acc in all_accounts:
                acc_id_str = str(acc.id)
                # Broad match: exact ID, or contains our account ID
                if acc_id_str == target_account_id or self._account_id in acc_id_str:
                    account = acc
                    break
            
            # Fallback: if we have accounts but none matched, use the first one 
            # (In a single-account setup this is almost always correct)
            if not account and all_accounts:
                account = all_accounts[0]
                logger.info(f"Account ID match failed, falling back to discovered account: {account.id}")

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
                            
                        logger.info(f"Account updated - Net Liquidation: {self._net_liquidation}, Buying Power: {self._buying_power}")
                    else:
                        logger.info(f"No individual balances found in account {target_account_id}")
                else:
                    logger.info(f"No balances found for account {target_account_id}")

                # Update open positions details using netting logic
                positions_data = []
                try:
                    all_cached_positions = list(self.node.cache.positions())
                    # Symbol -> {net_qty, total_upnl, total_cost, count}
                    netted: Dict[str, Dict[str, Any]] = {}
                    
                    for p in all_cached_positions:
                        if not p.is_closed:
                            symbol = str(p.instrument_id)
                            qty = float(p.quantity)
                            if p.side == PositionSide.SHORT:
                                qty = -qty
                                
                            upnl = 0.0
                            try:
                                if p.unrealized_pnl:
                                    upnl = float(p.unrealized_pnl.as_double())
                            except Exception: pass
                            
                            # Fallback: Calculate PnL from cache if native PnL is 0
                            if upnl == 0.0:
                                upnl = self._calculate_pnl_from_cache(p)

                            avg_px = 0.0
                            try:
                                if p.avg_px_open is not None:
                                    if hasattr(p.avg_px_open, "as_double"):
                                        avg_px = float(p.avg_px_open.as_double())
                                    else:
                                        avg_px = float(p.avg_px_open)
                            except Exception: pass
                            
                            # logger.info(f"Nautilus Position: {symbol}, qty={qty}, avg_px={avg_px}")
                            
                            logger.info(f"Position Detail: ID={p.id}, Inst={p.instrument_id}, Qty={qty}, AvgPx={avg_px}, UPnL={upnl}")

                            if symbol not in netted:
                                netted[symbol] = {
                                    "net_qty": 0.0,
                                    "total_upnl": 0.0,
                                    "total_cost": 0.0
                                }
                            
                            netted[symbol]["net_qty"] += qty
                            netted[symbol]["total_upnl"] += upnl
                            netted[symbol]["total_cost"] += (qty * avg_px)

                    # Convert netted map to list
                    for symbol, data in netted.items():
                        if abs(data["net_qty"]) > 1e-9:
                            avg_price = data["total_cost"] / data["net_qty"] if data["net_qty"] != 0 else 0
                            positions_data.append({
                                "symbol": symbol,
                                "quantity": data["net_qty"],
                                "avg_price": avg_price,
                                "unrealized_pnl": data["total_upnl"]
                            })
                            if data["total_upnl"] == 0.0:
                                logger.info(f"Position reported: {symbol}, Net Qty: {data['net_qty']}, Avg Price: {avg_price}, UPnL: {data['total_upnl']} (no price data)")
                            else:
                                logger.info(f"Position reported: {symbol}, Net Qty: {data['net_qty']}, Avg Price: {avg_price}, UPnL: {data['total_upnl']}")
                except Exception as e:
                    logger.error(f"Error processing positions: {e}", exc_info=True)
                
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
                    # Instead of checking just "InteractiveBrokers", aggregate all venues 
                    # registered to the IB execution client
                    total_exposure = 0.0
                    # ... exposure aggregation omitted for brevity ...
                    
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
                        trade_type = "buy" if order.side == OrderSide.BUY else "sell"
                        
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
            return trades
            
        except Exception as e:
            logger.error(f"Error fetching recent trades: {e}", exc_info=True)
            return []


    def _calculate_pnl_from_cache(self, position) -> float:
        """Calculate PnL using available market data from cache (quote ticks or bars)."""
        try:
            instrument_id = position.instrument_id
            entry_price = 0.0
            current_price = 0.0
            
            # Get entry price
            if position.avg_px_open is not None:
                if hasattr(position.avg_px_open, "as_double"):
                    entry_price = float(position.avg_px_open.as_double())
                else:
                    entry_price = float(position.avg_px_open)
            
            if entry_price == 0:
                logger.debug(f"PnL calc: {instrument_id} - no entry price")
                return 0.0
            
            # Try quote tick first
            quote = self.node.cache.quote_tick(instrument_id)
            if quote:
                bid = float(quote.bid_price)
                ask = float(quote.ask_price)
                if bid > 0 and ask > 0:
                    current_price = (bid + ask) / 2.0
                    logger.debug(f"PnL calc: {instrument_id} - got quote: {current_price}")
            
            # Try last bar if no quote - try multiple instrument ID variants
            if current_price == 0:
                # Get base symbol (without venue or external suffix)
                symbol_str = str(instrument_id.symbol) if hasattr(instrument_id, 'symbol') else str(instrument_id).split('.')[0]
                venue_str = str(instrument_id.venue) if hasattr(instrument_id, 'venue') else str(instrument_id).split('.')[-1].replace('-EXTERNAL', '')
                
                # Try different bar type combinations
                bar_specs = ["1-MINUTE-LAST-EXTERNAL", "1-MINUTE-MID-EXTERNAL", "1-MINUTE-BID-EXTERNAL", "30-MINUTE-LAST-EXTERNAL"]
                instrument_variants = [
                    str(instrument_id),
                    f"{symbol_str}.{venue_str}",
                ]
                
                logger.debug(f"PnL calc: {instrument_id} - trying bar variants: {instrument_variants}")
                
                for inst_var in instrument_variants:
                    if current_price > 0:
                        break
                    for bar_spec in bar_specs:
                        try:
                            bar_type_str = f"{inst_var}-{bar_spec}"
                            bar_type = BarType.from_str(bar_type_str)
                            bar = self.node.cache.bar(bar_type)
                            if bar:
                                current_price = float(bar.close)
                                logger.debug(f"PnL calc: {instrument_id} - got bar from {bar_type_str}: {current_price}")
                                break
                        except Exception as e:
                            logger.debug(f"PnL calc: {instrument_id} - failed {bar_type_str}: {e}")
                            continue
            
            if current_price == 0:
                logger.debug(f"PnL calc: {instrument_id} - no current price found")
                return 0.0
            
            # Get multiplier
            instrument = self.node.cache.instrument(instrument_id)
            multiplier = float(instrument.multiplier) if instrument and hasattr(instrument, 'multiplier') else 1.0
            
            qty = float(position.quantity)
            if position.side == PositionSide.LONG:
                pnl = (current_price - entry_price) * qty * multiplier

            else:
                pnl = (entry_price - current_price) * qty * multiplier
            
            logger.debug(f"PnL calc: {instrument_id} - entry={entry_price}, current={current_price}, qty={qty}, mult={multiplier} -> PnL={pnl}")
            return pnl
                
        except Exception as e:
            logger.warning(f"Could not calculate PnL for {position.instrument_id}: {e}")
            return 0.0

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
        
        # Check if already exists
        strategies = await self.strategy_manager.get_all_strategies_status()
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

    async def get_status(self) -> dict:
        """
        Get current connection and account status.
        """
        strategies = []
        if self.strategy_manager:
            strategies = await self.strategy_manager.get_all_strategies_status()

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
            "leverage": self._leverage,
            "recent_trades": self._recent_trades,
            "strategies": strategies,
        }

    async def update_status(self):
        """Update account state and connection health check"""
        if self.node:
             self._connected = True
        
        await self._update_account_state()
        self._recent_trades = await self._get_recent_trades(hours=24)
