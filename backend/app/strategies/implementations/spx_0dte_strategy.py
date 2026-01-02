from nautilus_trader.model.data import QuoteTick, TradeTick
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
        self.log(f"Initialized Spx0DteStraddleStrategy with config: {config}")
        self.log(f"Target Instrument ID: {self.instrument_id}")

    def on_start(self):
        self.log("Strategy start requested - calling subscribe_quote_ticks and subscribe_trade_ticks")
        self.subscribe_quote_ticks(self.instrument_id)
        self.subscribe_trade_ticks(self.instrument_id)
        self.log(f"Subscribed to quote and trade ticks for {self.instrument_id}. Waiting for data...")

    def on_trade_tick(self, tick: TradeTick):
        self.log(f"Received TradeTick: {tick}")
        if tick.instrument_id == self.instrument_id:
            price = tick.price
            self.log(f"Using Last Price from Trade: {price}")
            try:
                self.last_price = float(price)
            except Exception as e:
                 self.log(f"Error casting trade price {price} to float: {e}")

    def on_quote_tick(self, tick: QuoteTick):
        # Log absolutely everything received
        self.log(f"Received QuoteTick: {tick}")
        
        if tick.instrument_id == self.instrument_id:
            # Update price logic
            price = 0.0
            if tick.bid_price and tick.ask_price:
                 price = (tick.bid_price + tick.ask_price) / 2
                 self.log(f"Calculated Mid Price: {price} (Bid: {tick.bid_price}, Ask: {tick.ask_price})")
            elif tick.bid_price:
                 price = tick.bid_price
                 self.log(f"Using Bid Price: {price}")
            elif tick.ask_price:
                 price = tick.ask_price
                 self.log(f"Using Ask Price: {price}")
            else:
                 self.log("Tick received but no bid/ask price available")
            
            if price > 0:
                try:
                    self.last_price = float(price)
                except Exception as e:
                    self.log(f"Error casting price {price} to float: {e}")
                    pass
        else:
            self.log(f"Ignored tick for non-target instrument: {tick.instrument_id}")

    def log(self, message: str):
        # Determine internal log
        msg = f"[STRATEGY] {message}"
        # Keep a larger history of logs
        self.log_buffer.append(msg)
        if len(self.log_buffer) > 500: # Increased buffer size
            self.log_buffer.pop(0)
        logger.info(msg)

    def on_stop(self):
        self.log("Strategy stopped")
