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
        self._account_id: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

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
                # Get balances
                balances = account.balances()
                if balances:
                    # Use the first available balance
                    balance = balances[0]
                    self._net_liquidation = f"{balance.total.as_double():.2f} {balance.total.currency.code}"
                    logger.debug(f"Account updated - Net Liquidation: {self._net_liquidation}")
                else:
                    logger.debug(f"No balances found for account {target_account_id}")
            else:
                logger.warning(f"Account {target_account_id} not found in cache")

        except Exception as e:
            logger.error(f"Error updating account state: {e}")

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
            "net_liquidation": self._net_liquidation,
            "account_id": self._account_id,
        }

    async def update_status(self):
        """Update account state - call periodically for fresh data"""
        await self._update_account_state()
