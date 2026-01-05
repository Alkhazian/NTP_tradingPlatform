
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
import redis.asyncio as redis
import json
import logging
import asyncio

logger = logging.getLogger(__name__)

class SpxStreamerConfig(StrategyConfig):
    strategy_type: str = "SpxStreamer"
    instrument_id: str = "^SPX.CBOE"
    name: str = "SPX Streamer"
    id: str = "spx-streamer-01"
    redis_url: str = "redis://redis:6379/0"

# Import BaseStrategy from the strategies package to reuse the wrapper logic
# This allows us to use standard Nautilus plumbing while architecturally treating it as an Actor
from app.strategies.base import BaseStrategy

class SpxStreamer(BaseStrategy):
    """
    DataActor (implemented as Strategy) to stream SPX prices.
    Connects to SPX and broadcasts ticks to Redis for UI.
    """
    def __init__(self, config: SpxStreamerConfig, integration_manager=None, **kwargs):
        super().__init__(config, integration_manager)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.redis_client = None
        self._last_price = 0.0

    async def _log_to_ui(self, message: str, level: str = "info"):
        """Send log to UI via Redis"""
        logger.info(f"[SPX Streamer] {message}")
        try:
            if self.redis_client:
                await self.redis_client.publish(
                    "spx_stream_log",
                    json.dumps({
                        "type": "spx_log",
                        "timestamp": self.clock.timestamp_ns(), # nanoseconds
                        "message": message,
                        "level": level
                    })
                )
        except Exception as e:
            logger.error(f"[SPX Streamer] Failed to publish log to Redis: {e}")

    async def _broadcast_price(self, price: float):
        """Send price update to UI"""
        try:
            if self.redis_client:
                await self.redis_client.publish(
                    "spx_stream_price",
                    json.dumps({
                        "type": "spx_price",
                        "instrument": str(self.instrument_id),
                        "price": price,
                        "timestamp": self.clock.timestamp_ns()
                    })
                )
        except Exception as e:
            logger.error(f"[SPX Streamer] Failed to publish price to Redis: {e}")

    def on_start_safe(self):
        # Initialize Redis
        # Note: self.strategy_config holds the config object
        try:
            # Explicitly set decode_responses=True for consistency
            self.redis_client = redis.from_url(self.strategy_config.redis_url, decode_responses=True)
        except Exception as e:
             logger.error(f"[SPX Streamer] Failed to initialize Redis client: {e}")
        
        # Use task to log async
        asyncio.create_task(self._log_to_ui(f"Started SPX Streamer for {self.instrument_id}"))

        # Check if instrument exists, otherwise request it
        if self.cache.instrument(self.instrument_id):
            self.subscribe_quote_ticks(self.instrument_id)
        else:
            asyncio.create_task(self._log_to_ui(f"Instrument {self.instrument_id} not found in cache, requesting from IB..."))
            # Request explicit definition
            from nautilus_trader.model.identifiers import Venue
            self.request_instruments(
                venue=Venue("InteractiveBrokers"),
                params={
                    "ib_contracts": [
                        {"secType": "IND", "symbol": "SPX", "exchange": "CBOE", "currency": "USD"}
                    ]
                }
            )
            # Start polling just in case on_instrument_added doesn't fire
            asyncio.create_task(self._wait_for_instrument_and_subscribe())

    async def _wait_for_instrument_and_subscribe(self):
        """Fallback polling to ensure subscription if on_instrument_added doesn't fire immediately"""
        for i in range(30): # Try for 30 seconds
            if self.cache.instrument(self.instrument_id):
                 asyncio.create_task(self._log_to_ui(f"Instrument {self.instrument_id} found via polling, subscribing..."))
                 self.subscribe_quote_ticks(self.instrument_id)
                 return
            await asyncio.sleep(1)
        
        asyncio.create_task(self._log_to_ui(f"Timeout waiting for instrument {self.instrument_id} definition from IB"))

    def on_instrument_added(self, instrument: Instrument):
        if instrument.id == self.instrument_id:
            msg = f"Instrument {instrument.id} added (event), subscribing..."
            asyncio.create_task(self._log_to_ui(msg))
            self.subscribe_quote_ticks(self.instrument_id)

    def on_quote_tick(self, tick: QuoteTick):
        # Check if Bid or Ask is available, use Mid or Last logic
        bid = tick.bid_price.as_double()
        ask = tick.ask_price.as_double()
        price = 0.0

        if bid > 0 and ask > 0:
             price = (bid + ask) / 2
        elif bid > 0:
            price = bid
        elif ask > 0:
             price = ask
            
        if price > 0:
            self._last_price = price
            asyncio.create_task(self._broadcast_price(price))
            asyncio.create_task(self._log_to_ui(f"QuoteTick: {price:.2f}"))

    def on_stop_safe(self):
        if self.redis_client:
            asyncio.create_task(self._log_to_ui("Stopped SPX Streamer"))
            asyncio.create_task(self.redis_client.close())

    def get_state(self):
        return {}

    def set_state(self, state):
        pass
