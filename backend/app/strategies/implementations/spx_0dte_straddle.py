"""
SPX 0DTE Opening Straddle Strategy

This strategy implements a 0DTE (Zero Days to Expiration) straddle strategy
for the SPX index using NautilusTrader framework.

Key Features:
1. Subscribes to SPX.CBOE index price data
2. Handles IB's index data quirks (Last price instead of Bid/Ask)
3. Provides comprehensive logging for diagnostics
4. Manual start/stop control via API

Note on IB Index Data:
- IB transmits index data via reqMktData with Last Price only
- Size values may be invalid/huge for indices
- This strategy uses Bar data (5-second) as primary source for stability
- Falls back to QuoteTicks with synthetic Bid=Ask=Last if bars unavailable
"""

import logging
from decimal import Decimal
from typing import Optional, List
from datetime import datetime, timezone
from dataclasses import dataclass, field

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.data import Bar, BarType, QuoteTick, TradeTick
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.trading.strategy import Strategy


logger = logging.getLogger(__name__)


class Spx0DteStraddleConfig(StrategyConfig, frozen=True):
    """
    Configuration for SPX 0DTE Straddle Strategy.
    
    Attributes:
        instrument_id: The SPX instrument identifier (e.g., "SPX.CBOE")
        use_bars: If True, subscribe to 5-second bars instead of ticks
        bar_type: The bar type specification for bar subscriptions
    """
    instrument_id: str = "SPX.CBOE"
    use_bars: bool = True  # Prefer bars for index data stability
    bar_interval_seconds: int = 5
    order_id_tag: str = "SPX0DTE"


class Spx0DteStraddleStrategy(Strategy):
    """
    SPX 0DTE Opening Straddle Strategy.
    
    This strategy:
    1. Subscribes to SPX index price data
    2. Logs all received data for diagnostic purposes
    3. Tracks current price for UI display
    4. Designed for manual start/stop via API
    """
    
    def __init__(self, config: Spx0DteStraddleConfig) -> None:
        """
        Initialize the strategy.
        
        Note: Do not access clock or logger in __init__ - they're not 
        initialized until on_start().
        """
        super().__init__(config)
        
        # Parse instrument ID
        self._instrument_id = InstrumentId.from_str(config.instrument_id)
        
        # State variables
        self._instrument: Optional[Instrument] = None
        self._current_price: Optional[float] = None
        self._last_bid: Optional[float] = None
        self._last_ask: Optional[float] = None
        self._last_update_time: Optional[datetime] = None
        self._bar_type: Optional[BarType] = None
        
        # Strategy log for UI display
        self._strategy_logs: List[str] = []
        self._max_logs = 200
        
        # Tick/bar counters for diagnostics
        self._quote_tick_count = 0
        self._trade_tick_count = 0
        self._bar_count = 0
        self._data_count = 0
    
    def _log_strategy(self, message: str, level: str = "INFO") -> None:
        """
        Add a log entry for strategy UI display.
        Also logs to the system logger.
        """
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        entry = f"[{timestamp}] [{level}] {message}"
        self._strategy_logs.append(entry)
        
        # Keep only recent entries
        if len(self._strategy_logs) > self._max_logs:
            self._strategy_logs = self._strategy_logs[-self._max_logs:]
        
        # Log to NautilusTrader logger
        if hasattr(self, '_log') and self._log:
            if level == "ERROR":
                self._log.error(message)
            elif level == "WARNING":
                self._log.warning(message)
            elif level == "DEBUG":
                self._log.debug(message)
            else:
                self._log.info(message)
    
    def get_strategy_logs(self) -> List[str]:
        """Get strategy logs for UI display."""
        return self._strategy_logs.copy()
    
    def clear_logs(self) -> None:
        """Clear strategy logs."""
        self._strategy_logs.clear()
    
    def get_current_price(self) -> Optional[float]:
        """Get the current SPX price."""
        return self._current_price
    
    def get_status(self) -> dict:
        """Get strategy status for UI display."""
        return {
            "strategy_id": str(self.id) if hasattr(self, 'id') else "SPX0DTE",
            "instrument_id": str(self._instrument_id),
            "current_price": self._current_price,
            "last_bid": self._last_bid,
            "last_ask": self._last_ask,
            "last_update": self._last_update_time.isoformat() if self._last_update_time else None,
            "is_running": self.is_running if hasattr(self, 'is_running') else False,
            "quote_tick_count": self._quote_tick_count,
            "trade_tick_count": self._trade_tick_count,
            "bar_count": self._bar_count,
            "data_count": self._data_count,
            "has_instrument": self._instrument is not None,
        }
    
    def on_start(self) -> None:
        """
        Called when the strategy is started.
        
        This is where we:
        1. Load the instrument from cache
        2. Subscribe to market data
        3. Initialize timers if needed
        """
        self._log_strategy("="*60)
        self._log_strategy("SPX 0DTE Straddle Strategy STARTING")
        self._log_strategy(f"Instrument ID: {self._instrument_id}")
        self._log_strategy(f"Use Bars: {self.config.use_bars}")
        self._log_strategy("="*60)
        
        # Try to get instrument from cache
        self._instrument = self.cache.instrument(self._instrument_id)
        
        if self._instrument:
            self._log_strategy(f"✓ Instrument loaded from cache: {self._instrument}")
            self._log_strategy(f"  - Asset class: {self._instrument.asset_class}")
            self._log_strategy(f"  - Quote currency: {self._instrument.quote_currency}")
            if hasattr(self._instrument, 'price_precision'):
                self._log_strategy(f"  - Price precision: {self._instrument.price_precision}")
        else:
            self._log_strategy(
                f"⚠ Instrument {self._instrument_id} NOT found in cache! "
                "Data subscription may fail.",
                "WARNING"
            )
            # List available instruments for debugging
            available = self.cache.instrument_ids()
            self._log_strategy(f"Available instruments in cache: {len(available)}")
            for inst_id in list(available)[:10]:  # Show first 10
                self._log_strategy(f"  - {inst_id}")
        
        # Subscribe to data based on configuration
        if self.config.use_bars:
            self._subscribe_to_bars()
        else:
            self._subscribe_to_ticks()
        
        self._log_strategy("Strategy startup complete. Waiting for data...")
    
    def _subscribe_to_bars(self) -> None:
        """Subscribe to 5-second bars for SPX."""
        try:
            # Create bar type specification
            # Format: INSTRUMENT-STEP-STEP_TYPE-AGGREGATION_SOURCE
            # Note: BarType parser doesn't support ^ prefix, so we need to strip it
            instrument_str = str(self._instrument_id)
            
            # Remove ^ prefix for BarType if present (IB uses ^ for indices)
            if instrument_str.startswith("^"):
                clean_instrument_str = instrument_str[1:]  # Remove ^
                self._log_strategy("Note: Stripping ^ prefix from instrument ID for BarType compatibility")
            else:
                clean_instrument_str = instrument_str
            
            bar_spec = f"{clean_instrument_str}-{self.config.bar_interval_seconds}-SECOND-LAST"
            self._bar_type = BarType.from_str(bar_spec)
            
            self._log_strategy(f"SENDING: subscribe_bars({self._bar_type})")
            self.subscribe_bars(self._bar_type)
            self._log_strategy(f"✓ Subscribed to bars: {self._bar_type}")
            
        except Exception as e:
            self._log_strategy(f"✗ Failed to subscribe to bars: {e}", "ERROR")
            # Fallback to ticks
            self._log_strategy("Falling back to quote tick subscription...", "WARNING")
            self._subscribe_to_ticks()
    
    def _subscribe_to_ticks(self) -> None:
        """Subscribe to quote ticks for SPX."""
        try:
            self._log_strategy(f"SENDING: subscribe_quote_ticks({self._instrument_id})")
            self.subscribe_quote_ticks(self._instrument_id)
            self._log_strategy(f"✓ Subscribed to quote ticks: {self._instrument_id}")
            
        except Exception as e:
            self._log_strategy(f"✗ Failed to subscribe to quote ticks: {e}", "ERROR")
            
            # Also try trade ticks as fallback (IB may send Last as TradeTick)
            try:
                self._log_strategy(f"SENDING: subscribe_trade_ticks({self._instrument_id})")
                self.subscribe_trade_ticks(self._instrument_id)
                self._log_strategy(f"✓ Subscribed to trade ticks: {self._instrument_id}")
            except Exception as e2:
                self._log_strategy(f"✗ Failed to subscribe to trade ticks: {e2}", "ERROR")
    
    def on_stop(self) -> None:
        """
        Called when the strategy is stopped.
        
        Cleanup subscriptions and log final stats.
        """
        self._log_strategy("="*60)
        self._log_strategy("SPX 0DTE Straddle Strategy STOPPING")
        self._log_strategy(f"Final price: {self._current_price}")
        self._log_strategy(f"Total quote ticks received: {self._quote_tick_count}")
        self._log_strategy(f"Total trade ticks received: {self._trade_tick_count}")
        self._log_strategy(f"Total bars received: {self._bar_count}")
        self._log_strategy(f"Total custom data received: {self._data_count}")
        self._log_strategy("="*60)
        
        # Unsubscribe from data
        try:
            if self._bar_type:
                self.unsubscribe_bars(self._bar_type)
                self._log_strategy(f"Unsubscribed from bars: {self._bar_type}")
        except Exception as e:
            self._log_strategy(f"Error unsubscribing from bars: {e}", "WARNING")
        
        try:
            self.unsubscribe_quote_ticks(self._instrument_id)
            self._log_strategy(f"Unsubscribed from quote ticks: {self._instrument_id}")
        except Exception:
            pass  # May not have been subscribed
        
        try:
            self.unsubscribe_trade_ticks(self._instrument_id)
            self._log_strategy(f"Unsubscribed from trade ticks: {self._instrument_id}")
        except Exception:
            pass  # May not have been subscribed
    
    def on_quote_tick(self, tick: QuoteTick) -> None:
        """
        Handle incoming quote tick data.
        
        For SPX index, IB may send synthetic ticks or the adapter
        may have created them from Last price.
        """
        self._quote_tick_count += 1
        
        # Extract prices
        bid_price = float(tick.bid_price)
        ask_price = float(tick.ask_price)
        bid_size = float(tick.bid_size)
        ask_size = float(tick.ask_size)
        
        # Update state
        self._last_bid = bid_price
        self._last_ask = ask_price
        self._current_price = (bid_price + ask_price) / 2  # Mid price
        self._last_update_time = datetime.now(timezone.utc)
        
        # Detailed logging for diagnostics
        self._log_strategy(
            f"RECEIVED QuoteTick #{self._quote_tick_count}: "
            f"bid={bid_price:.2f} (size={bid_size}), "
            f"ask={ask_price:.2f} (size={ask_size}), "
            f"mid={self._current_price:.2f}",
            "DEBUG"
        )
        
        # Log object type for debugging IB adapter behavior
        self._log_strategy(
            f"  Object type: {type(tick).__module__}.{type(tick).__name__}",
            "DEBUG"
        )
    
    def on_trade_tick(self, tick: TradeTick) -> None:
        """
        Handle incoming trade tick data.
        
        For SPX index, IB typically sends Last price as this type.
        This is the primary data source for indices.
        """
        self._trade_tick_count += 1
        
        # Extract price and size
        price = float(tick.price)
        size = float(tick.size)
        
        # Update state - use trade price as current price
        self._current_price = price
        self._last_update_time = datetime.now(timezone.utc)
        
        # Log the tick
        self._log_strategy(
            f"RECEIVED TradeTick #{self._trade_tick_count}: "
            f"price={price:.2f}, size={size}",
            "DEBUG"
        )
        
        # Log object type
        self._log_strategy(
            f"  Object type: {type(tick).__module__}.{type(tick).__name__}",
            "DEBUG"
        )
        
        # Check for potentially invalid size (common with IB indices)
        if size > 1_000_000_000:
            self._log_strategy(
                f"  ⚠ Large size value detected ({size}) - typical for IB index data",
                "WARNING"
            )
    
    def on_bar(self, bar: Bar) -> None:
        """
        Handle incoming bar data.
        
        5-second bars provide cleaner data for indices compared to ticks.
        """
        self._bar_count += 1
        
        # Extract bar values
        open_price = float(bar.open)
        high_price = float(bar.high)
        low_price = float(bar.low)
        close_price = float(bar.close)
        volume = float(bar.volume)
        
        # Update state with close price
        self._current_price = close_price
        self._last_update_time = datetime.now(timezone.utc)
        
        # Log bar data
        self._log_strategy(
            f"RECEIVED Bar #{self._bar_count}: "
            f"O={open_price:.2f} H={high_price:.2f} L={low_price:.2f} C={close_price:.2f} "
            f"V={volume:.0f}"
        )
        
        # Log object type
        self._log_strategy(
            f"  Bar type: {bar.bar_type}",
            "DEBUG"
        )
    
    def on_data(self, data) -> None:
        """
        Handle any custom data.
        
        This catches any data that doesn't match standard types.
        Useful for debugging what IB adapter actually sends.
        """
        self._data_count += 1
        
        self._log_strategy(
            f"RECEIVED Custom Data #{self._data_count}: "
            f"type={type(data).__module__}.{type(data).__name__}",
            "DEBUG"
        )
        
        # Try to extract common attributes
        if hasattr(data, 'price'):
            self._log_strategy(f"  price attribute: {data.price}", "DEBUG")
        if hasattr(data, 'value'):
            self._log_strategy(f"  value attribute: {data.value}", "DEBUG")
    
    def on_instrument(self, instrument: Instrument) -> None:
        """
        Handle instrument updates.
        
        Called when instrument definition is received/updated.
        """
        self._instrument = instrument
        self._log_strategy(
            f"RECEIVED Instrument: {instrument.id} "
            f"(type: {type(instrument).__name__})"
        )
    
    def on_reset(self) -> None:
        """Reset strategy state."""
        self._log_strategy("Strategy RESET called")
        self._current_price = None
        self._last_bid = None
        self._last_ask = None
        self._last_update_time = None
        self._quote_tick_count = 0
        self._trade_tick_count = 0
        self._bar_count = 0
        self._data_count = 0


# Factory function for creating strategy with default config
def create_spx_0dte_strategy(
    instrument_id: str = "SPX.CBOE",
    use_bars: bool = True,
    order_id_tag: str = "SPX0DTE001"
) -> Spx0DteStraddleStrategy:
    """
    Factory function to create an SPX 0DTE Straddle Strategy.
    
    Args:
        instrument_id: The SPX instrument ID
        use_bars: Whether to use bars (True) or ticks (False)
        order_id_tag: Unique tag for orders from this strategy
        
    Returns:
        Configured strategy instance
    """
    config = Spx0DteStraddleConfig(
        instrument_id=instrument_id,
        use_bars=use_bars,
        order_id_tag=order_id_tag,
    )
    return Spx0DteStraddleStrategy(config=config)
