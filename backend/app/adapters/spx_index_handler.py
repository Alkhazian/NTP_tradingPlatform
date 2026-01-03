"""
SPX Index Data Handler for Interactive Brokers

This module provides a custom data handler to work around the limitations
of IB's data feed for index instruments (like SPX).

IB transmits index data through legacy reqMktData stream where:
- Price arrives as Last Price (not Bid/Ask)
- Size values can be extremely large or invalid for indices
- Standard NautilusTrader IBDataClient expects QuoteTick/TradeTick format

This handler:
1. Creates synthetic QuoteTicks where Bid = Ask = Last price
2. Filters out invalid size values for indices
3. Provides RealTimeBars as an alternative data source
"""

import logging
from decimal import Decimal
from typing import Optional, Callable, List
from datetime import datetime, timezone
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SPXPriceUpdate:
    """
    Custom data class for SPX price updates.
    Designed to work with NautilusTrader's custom data system.
    """
    price: float
    timestamp: datetime
    source: str  # 'LAST', 'BID', 'ASK', 'BAR'
    raw_size: Optional[float] = None
    bar_data: Optional[dict] = None  # OHLC for bar data
    
    @property
    def ts_event(self) -> int:
        """UNIX timestamp (nanoseconds) when the data event occurred."""
        return int(self.timestamp.timestamp() * 1_000_000_000)
    
    @property
    def ts_init(self) -> int:
        """UNIX timestamp (nanoseconds) when the object was initialized."""
        return int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)


@dataclass
class SPXIndexHandler:
    """
    Handler for processing SPX index data from Interactive Brokers.
    
    This class provides methods to:
    1. Transform IB's raw tick data into usable formats
    2. Filter invalid size values
    3. Create synthetic quote ticks from last price
    4. Handle 5-second RealTimeBars as alternative to ticks
    """
    
    # Configuration
    instrument_id: str = "SPX.CBOE"
    
    # State
    last_price: Optional[float] = None
    last_bid: Optional[float] = None
    last_ask: Optional[float] = None
    last_update_time: Optional[datetime] = None
    
    # Callbacks
    on_price_update: Optional[Callable[[SPXPriceUpdate], None]] = None
    
    # Logging
    _log_entries: List[str] = field(default_factory=list)
    max_log_entries: int = 100
    
    def log(self, message: str, level: str = "INFO") -> None:
        """Add a log entry for strategy UI display."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        entry = f"[{timestamp}] [{level}] {message}"
        self._log_entries.append(entry)
        
        # Keep only recent entries
        if len(self._log_entries) > self.max_log_entries:
            self._log_entries = self._log_entries[-self.max_log_entries:]
        
        # Also log to Python logger
        log_func = getattr(logger, level.lower(), logger.info)
        log_func(f"[SPXHandler] {message}")
    
    def get_logs(self) -> List[str]:
        """Get all log entries."""
        return self._log_entries.copy()
    
    def clear_logs(self) -> None:
        """Clear log entries."""
        self._log_entries.clear()
    
    def is_valid_index_size(self, size: float) -> bool:
        """
        Check if a size value is valid for an index instrument.
        
        IB often sends extremely large or technical numbers for index sizes.
        For indices, we typically ignore size or set it to a default value.
        
        Args:
            size: The size value from IB
            
        Returns:
            True if the size appears valid, False otherwise
        """
        # Common invalid size patterns for indices
        if size is None:
            return False
        
        # Size of 0 is valid for indices (no actual trading volume)
        if size == 0:
            return True
        
        # Extremely large values are typically invalid
        if abs(size) > 1_000_000_000:  # 1 billion
            self.log(f"Ignoring invalid size: {size}", "DEBUG")
            return False
        
        # Negative sizes are invalid
        if size < 0:
            self.log(f"Ignoring negative size: {size}", "DEBUG")
            return False
        
        return True
    
    def normalize_size_for_index(self, size: float) -> float:
        """
        Normalize size for index instruments.
        
        For indices, we set size to 1 to allow the tick to pass validation
        while indicating that volume data is not meaningful.
        """
        if not self.is_valid_index_size(size):
            return 1.0  # Default size for indices
        return 1.0  # Always use 1 for indices since volume is not meaningful
    
    def process_tick_price(
        self,
        tick_type: int,
        price: float,
        size: Optional[float] = None,
        timestamp: Optional[datetime] = None
    ) -> Optional[SPXPriceUpdate]:
        """
        Process a tick price from IB.
        
        IB tick types (from TWS API):
        - 1: BID
        - 2: ASK
        - 4: LAST
        - 6: HIGH
        - 7: LOW
        - 9: CLOSE
        
        For SPX index, we primarily get LAST prices.
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        
        # Map tick types
        tick_type_names = {
            1: "BID",
            2: "ASK",
            4: "LAST",
            6: "HIGH",
            7: "LOW",
            9: "CLOSE"
        }
        
        source = tick_type_names.get(tick_type, f"UNKNOWN_{tick_type}")
        normalized_size = self.normalize_size_for_index(size) if size else 1.0
        
        self.log(
            f"Received tickPrice: type={source}({tick_type}), "
            f"price={price}, raw_size={size}, normalized_size={normalized_size}",
            "DEBUG"
        )
        
        # Update internal state based on tick type
        if tick_type == 1:  # BID
            self.last_bid = price
        elif tick_type == 2:  # ASK
            self.last_ask = price
        elif tick_type == 4:  # LAST
            self.last_price = price
            self.last_update_time = timestamp
            
            # Create price update
            update = SPXPriceUpdate(
                price=price,
                timestamp=timestamp,
                source=source,
                raw_size=size
            )
            
            self.log(f"SPX price update: {price} (source: {source})")
            
            if self.on_price_update:
                self.on_price_update(update)
            
            return update
        
        return None
    
    def create_synthetic_quote_tick(self) -> Optional[dict]:
        """
        Create a synthetic quote tick from the last price.
        
        For index instruments where only Last price is available,
        we create a QuoteTick with Bid = Ask = Last.
        
        Returns:
            Dict with quote tick data, or None if no price available
        """
        if self.last_price is None:
            return None
        
        # Use last price for both bid and ask
        bid = self.last_bid if self.last_bid else self.last_price
        ask = self.last_ask if self.last_ask else self.last_price
        
        quote = {
            "instrument_id": self.instrument_id,
            "bid_price": bid,
            "ask_price": ask,
            "bid_size": 1,  # Normalized size for indices
            "ask_size": 1,
            "ts_event": int(self.last_update_time.timestamp() * 1_000_000_000) if self.last_update_time else 0,
            "ts_init": int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)
        }
        
        self.log(f"Created synthetic QuoteTick: bid={bid}, ask={ask}")
        return quote
    
    def process_realtime_bar(
        self,
        bar_time: datetime,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        volume: int,
        wap: float,
        count: int
    ) -> SPXPriceUpdate:
        """
        Process a 5-second RealTimeBar from IB.
        
        This is an alternative to tick data that provides cleaner data
        for index instruments.
        """
        self.last_price = close_price
        self.last_update_time = bar_time
        
        bar_data = {
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
            "wap": wap,
            "count": count
        }
        
        self.log(
            f"RealTimeBar: O={open_price:.2f} H={high_price:.2f} "
            f"L={low_price:.2f} C={close_price:.2f}"
        )
        
        update = SPXPriceUpdate(
            price=close_price,
            timestamp=bar_time,
            source="BAR",
            bar_data=bar_data
        )
        
        if self.on_price_update:
            self.on_price_update(update)
        
        return update
    
    def get_current_price(self) -> Optional[float]:
        """Get the current SPX price."""
        return self.last_price
    
    def get_status(self) -> dict:
        """Get handler status for UI display."""
        return {
            "instrument_id": self.instrument_id,
            "current_price": self.last_price,
            "last_bid": self.last_bid,
            "last_ask": self.last_ask,
            "last_update": self.last_update_time.isoformat() if self.last_update_time else None,
            "has_data": self.last_price is not None
        }
