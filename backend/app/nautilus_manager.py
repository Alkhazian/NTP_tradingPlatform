import asyncio
import logging
import os
from typing import Optional
from nautilus_trader.adapters.interactive_brokers.config import (
    InteractiveBrokersDataClientConfig,
    InteractiveBrokersExecClientConfig,
    InteractiveBrokersInstrumentProviderConfig,
)
from nautilus_trader.adapters.interactive_brokers.factories import (
    InteractiveBrokersLiveDataClientFactory,
    InteractiveBrokersLiveExecClientFactory,
)
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
        self._account_currency = "USD"
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._buying_power = "0.0"
        self._day_realized_pnl = "0.0"

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
            
            # Start the node in the background
            asyncio.create_task(self.node.run_async())

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
            else:
                logger.warning(f"Account {target_account_id} not found in cache")

        except Exception as e:
            logger.error(f"Error updating account state: {e}", exc_info=True)

    async def stop(self):

        """Stop the NautilusTrader TradingNode"""
        if self.node:
            logger.info("Stopping NautilusTrader TradingNode")
            await self.node.stop_async()
            self._connected = False
            logger.info("NautilusTrader TradingNode stopped")

    def get_status(self) -> dict:
        """
        Get current connection and account status.
        Maintains compatibility with legacy IBConnector interface.
        """
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
        }

    async def update_status(self):
        """Update account state - call periodically for fresh data"""
        await self._update_account_state()
