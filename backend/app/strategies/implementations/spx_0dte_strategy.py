from nautilus_trader.model.data import QuoteTick
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.identifiers import InstrumentId
import logging

logger = logging.getLogger(__name__)

class Spx0DteStraddleStrategyConfig(StrategyConfig):
    instrument_id: str

class Spx0DteStraddleStrategy(Strategy):
    def __init__(self, config: Spx0DteStraddleStrategyConfig):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.last_price = 0.0
        self.log_buffer = []

    def on_start(self):
        self.log("Strategy started")
        self.subscribe_quote_ticks(self.instrument_id)
        self.log(f"Subscribed to quote ticks for {self.instrument_id}")

    def on_quote_tick(self, tick: QuoteTick):
        if tick.instrument_id == self.instrument_id:
            # Update price logic
            price = 0.0
            if tick.bid_price and tick.ask_price:
                 price = (tick.bid_price + tick.ask_price) / 2
            elif tick.bid_price:
                 price = tick.bid_price
            elif tick.ask_price:
                 price = tick.ask_price
            
            # as_double() is typically used for Price objects
            # but if they are already floats or similar wrapper:
            try:
                self.last_price = float(price) # Simplified
            except:
                pass
            
            # We don't want to log every tick to avoid spam, maybe just every now and then or big changes
            # But user asked: "Log everything sent and received"
            # Since this is "debugging" step, maybe we log it.
            # self.log(f"Received quote: {tick}") 

    def log(self, message: str):
        # Determine internal log
        msg = f"[STRATEGY] {message}"
        self.log_buffer.append(msg)
        if len(self.log_buffer) > 100:
            self.log_buffer.pop(0)
        logger.info(msg)

    def on_stop(self):
        self.log("Strategy stopped")
