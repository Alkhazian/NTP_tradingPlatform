"""
SPX 0DTE Opening Straddle Strategy

This strategy implements a 0DTE (Zero Days to Expiration) straddle strategy
for the SPX index using NautilusTrader framework.

Key Features:
1. Subscribes to SPX.CBOE index price data
2. Handles IB's index data quirks (Last price instead of Bid/Ask)
3. Provides comprehensive logging for diagnostics
4. Manual start/stop control via API
5. Dynamic selection of Straddle contracts (Call/Put at ATM)

Note on IB Index Data:
- IB transmits index data via reqMktData with Last Price only
- Size values may be invalid/huge for indices
- This strategy uses Bar data (5-second) as primary source for stability
- Falls back to QuoteTicks with synthetic Bid=Ask=Last if bars unavailable
"""

import logging
from decimal import Decimal
from typing import Optional, List, Set, Tuple
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.data import Bar, BarType, QuoteTick, TradeTick
from nautilus_trader.model.instruments import Instrument, OptionInstrument, OptionType, InstrumentFilter
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.trading.strategy import Strategy


logger = logging.getLogger(__name__)


class Spx0DteStraddleConfig(StrategyConfig, frozen=True):
    """
    Configuration for SPX 0DTE Straddle Strategy.
    
    Attributes:
        instrument_id: The SPX instrument identifier (e.g., "SPX.CBOE")
        use_bars: If True, subscribe to 5-second bars instead of ticks
        bar_interval_seconds: Bar interval in seconds
        order_id_tag: Tag for orders
        strike_offset: Offset from ATM strike (default 0)
        days_to_expiry: Days to expiry (default 0 for 0DTE)
        refresh_interval_seconds: How often to refresh contract search
    """
    instrument_id: str = "SPX.CBOE"
    use_bars: bool = True  # Prefer bars for index data stability
    bar_interval_seconds: int = 5
    order_id_tag: str = "SPX0DTE"
    strike_offset: int = 0
    days_to_expiry: int = 0
    refresh_interval_seconds: int = 60


class Spx0DteStraddleStrategy(Strategy):
    """
    SPX 0DTE Opening Straddle Strategy.
    
    This strategy:
    1. Subscribes to SPX index price data
    2. Logs all received data for diagnostic purposes
    3. Tracks current price for UI display
    4. Dynamically selects Call/Put contracts for the Straddle
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
        
        # Contract Selection State
        self._current_call: Optional[OptionInstrument] = None
        self._current_put: Optional[OptionInstrument] = None
        self._last_contract_search_time: Optional[datetime] = None
        self._distance_to_strike: Optional[float] = None
        
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
            # Selected Contracts Data
            "current_call_id": str(self._current_call.id) if self._current_call else None,
            "current_put_id": str(self._current_put.id) if self._current_put else None,
            "current_call_verified": self._check_instrument_verified(self._current_call),
            "current_put_verified": self._check_instrument_verified(self._current_put),
            "distance_to_strike": self._distance_to_strike,
        }

    def _check_instrument_verified(self, instrument: Optional[Instrument]) -> bool:
        """Check if instrument is verified in cache."""
        if not instrument:
            return False
        cached = self.cache.instrument(instrument.id)
        return cached is not None

    def on_start(self) -> None:
        """
        Called when the strategy is started.
        """
        self._log_strategy("="*60)
        self._log_strategy("SPX 0DTE Straddle Strategy STARTING")
        self._log_strategy(f"Instrument ID: {self._instrument_id}")
        self._log_strategy(f"Use Bars: {self.config.use_bars}")
        self._log_strategy(f"Config: Strike Offset={self.config.strike_offset}, DTE={self.config.days_to_expiry}")
        self._log_strategy("="*60)
        
        # Try to get instrument from cache
        self._instrument = self.cache.instrument(self._instrument_id)
        
        if self._instrument:
            self._log_strategy(f"✓ Instrument loaded from cache: {self._instrument}")
        else:
            self._log_strategy(
                f"⚠ Underlying Instrument {self._instrument_id} NOT found in cache! ",
                "WARNING"
            )

        # Trigger instrument recovery/discovery for options
        # This is critical to populate self.cache with option contracts
        try:
            self._log_strategy(f"Requesting Option Chain for {self._instrument_id}...")
            self.request_instruments(
                InstrumentFilter(
                    asset_class=AssetClass.OPTION,
                    underlying_id=self._instrument_id,
                )
            )
            self._log_strategy("✓ Instrument request sent")
        except Exception as e:
            self._log_strategy(f"⚠ Failed to request instruments: {e}", "WARNING")

        # Subscribe to data
        if self.config.use_bars:
            self._subscribe_to_bars()
        else:
            self._subscribe_to_ticks()
            
        # Initial search for contracts if we have a price (e.g. from existing cache/history)
        # Note: We really need a fresh price, so we might wait for first data tick
        
        self._log_strategy("Strategy startup complete. Waiting for data...")
    
    def _find_straddle_instruments(self) -> None:
        """
        Find best matching Call and Put contracts based on current SPX price.
        """
        if self._current_price is None:
            return

        # Check refresh interval
        now = datetime.now(timezone.utc)
        if self._last_contract_search_time:
            elapsed = (now - self._last_contract_search_time).total_seconds()
            if elapsed < self.config.refresh_interval_seconds:
                return

        self._last_contract_search_time = now
        self._log_strategy(f"Searching for contracts at SPX={self._current_price:.2f}...")

        # Calculate logic
        # 1. Target Strike
        # SPX strikes are usually every 5 or 10 or 25 points.
        # Assuming we want nearest 5.
        # Strike = round(price / 5) * 5 + offset
        
        base_strike = round(self._current_price / 5) * 5
        target_strike = base_strike + self.config.strike_offset
        
        # 2. Target Expiry
        # Logic for days_to_expiry (0DTE means today)
        # Would typically check expiration dates.
        # For simulation/IB, we might need request_instruments logic.
        
        # Filter instruments from Cache first
        # We need ALL option instruments for this underlying.
        # Note: In a real scenario, we might need to REQUEST them first if not in cache.
        
        obs_instruments = self.cache.instruments()
        # Filter for Options on SPX
        
        best_call: Optional[OptionInstrument] = None
        best_put: Optional[OptionInstrument] = None
        
        # Assuming we have instruments in cache. If not, this loop won't find them.
        # In a real dynamic loading scenario, we would define an InstrumentFilter and call request_instruments.
        # But here we search internal cache as requested as primary step.
        
        candidates = []
        for inst in obs_instruments:
            if isinstance(inst, OptionInstrument):
                 # Check underlying (simplified check)
                 # Note: inst.underlying_id might be different if using different symbology
                 # We'll check if symbol matches or underlying matches
                 if inst.underlying_id == self._instrument_id or (self._instrument and inst.underlying_id == self._instrument.id):
                     # Filter for SPXW (Weekly/Daily options) as requested
                     # We check both direct attribute and info dictionary (common for IB adapter)
                     t_class = getattr(inst, "trading_class", None)
                     
                     # Fallback to info dict if not found as attribute
                     if not t_class and hasattr(inst, "info"):
                         t_class = inst.info.get("trading_class") or inst.info.get("tradingClass")

                     if t_class == "SPXW":
                         candidates.append(inst)
                     elif not t_class:
                         # If no trading class info, we might log it once or strictly require it. 
                         # For now, let's be strict as per requirement, but maybe warn if we find nothing.
                         pass

        if not candidates:
             # If no candidates in cache, we use request_instruments provided by Nautilus
             # But this is a blocking check here for simplicity or we log.
             if self._instrument:
                 self._log_strategy("No option candidates in cache, waiting for publication...", "DEBUG")
             return

        # Filter by expiry and strike
        # For 0DTE, we look for expiry matching today (or configured days)
        
        matching_expiry = []
        
        # Calculate target date
        today = datetime.now(timezone.utc).date()
        if self.config.days_to_expiry == 0:
            target_date = today
        else:
            target_date = today + timedelta(days=self.config.days_to_expiry)
            
        # Find instruments with matching or closest expiry
        for inst in candidates:
             if inst.expiry_date:
                  inst_date = inst.expiry_date.date() if isinstance(inst.expiry_date, datetime) else inst.expiry_date
                  
                  # Exact match logic
                  if inst_date == target_date:
                       matching_expiry.append(inst)
        
        # If no exact match, try to find the absolute nearest expiry
        if not matching_expiry and candidates:
             self._log_strategy(f"No exact expiry match for {target_date}, finding nearest...", "DEBUG")
             closest_expiry_diff = float('inf')
             for inst in candidates:
                  if inst.expiry_date:
                       inst_date = inst.expiry_date.date() if isinstance(inst.expiry_date, datetime) else inst.expiry_date
                       diff = abs((inst_date - target_date).days)
                       if diff < closest_expiry_diff:
                            closest_expiry_diff = diff
             
             # Collect all with this closest diff
             for inst in candidates:
                  if inst.expiry_date:
                       inst_date = inst.expiry_date.date() if isinstance(inst.expiry_date, datetime) else inst.expiry_date
                       diff = abs((inst_date - target_date).days)
                       if diff == closest_expiry_diff:
                            matching_expiry.append(inst)

        if not matching_expiry:
             self._log_strategy("No option candidates found matching expiry criteria.", "WARNING")
             return

        # Find closest strike to target_strike
        closest_diff = float('inf')
        selected_strike = None
        
        for inst in matching_expiry:
             diff = abs(float(inst.strike_price) - target_strike)
             if diff < closest_diff:
                 closest_diff = diff
                 selected_strike = inst.strike_price
        
        if selected_strike is None:
            self._log_strategy("Could not find any suitable strike.", "WARNING")
            return
            
        # Get Call and Put for this strike
        for inst in matching_expiry:
            if inst.strike_price == selected_strike:
                if inst.option_type == OptionType.CALL:
                    best_call = inst
                elif inst.option_type == OptionType.PUT:
                    best_put = inst
        
        # Update selection if changed
        if best_call and best_put:
             # Verify in cache (double check)
             call_verified = self._check_instrument_verified(best_call)
             put_verified = self._check_instrument_verified(best_put)
             
             if not call_verified or not put_verified:
                 self._log_strategy(f"Selected pair not fully verified in cache: {best_call.id}/{best_put.id}", "WARNING")
                 return

             if self._current_call != best_call or self._current_put != best_put:
                 self._current_call = best_call
                 self._current_put = best_put
                 self._distance_to_strike = self._current_price - float(selected_strike)
                 
                 # Log selection
                 expiry_date = best_call.expiry_date.strftime("%Y-%m-%d") if best_call.expiry_date else "N/A"
                 self._log_strategy(f"New Straddle Pair Selected: CALL/PUT SPX {selected_strike} {expiry_date}")
                 self._log_strategy(f"  Call ID: {best_call.id}")
                 self._log_strategy(f"  Put ID: {best_put.id}")
                 
                 # Log details for verification
                 if hasattr(best_call, 'price_precision'):
                      self._log_strategy(f"  Call Precision: {best_call.price_precision}, Min Qty: {best_call.min_quantity}", "DEBUG")

    def _subscribe_to_bars(self) -> None:
        """Subscribe to 5-second bars for SPX."""
        try:
            # Create bar type specification
            instrument_str = str(self._instrument_id)
            if instrument_str.startswith("^"):
                clean_instrument_str = instrument_str[1:]
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
            try:
                self._log_strategy(f"SENDING: subscribe_trade_ticks({self._instrument_id})")
                self.subscribe_trade_ticks(self._instrument_id)
                self._log_strategy(f"✓ Subscribed to trade ticks: {self._instrument_id}")
            except Exception as e2:
                self._log_strategy(f"✗ Failed to subscribe to trade ticks: {e2}", "ERROR")
    
    def on_stop(self) -> None:
        """
        Called when the strategy is stopped.
        """
        self._log_strategy("="*60)
        self._log_strategy("SPX 0DTE Straddle Strategy STOPPING")
        self._log_strategy(f"Final price: {self._current_price}")
        self._log_strategy("="*60)
        
        # Unsubscribe
        try:
            if self._bar_type:
                self.unsubscribe_bars(self._bar_type)
        except Exception:
            pass
        
        try:
            self.unsubscribe_quote_ticks(self._instrument_id)
        except Exception:
            pass
        
        try:
            self.unsubscribe_trade_ticks(self._instrument_id)
        except Exception:
            pass

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Handle incoming quote tick data."""
        self._quote_tick_count += 1
        
        # Extract prices
        bid_price = float(tick.bid_price)
        ask_price = float(tick.ask_price)
        
        # Update state
        self._last_bid = bid_price
        self._last_ask = ask_price
        self._current_price = (bid_price + ask_price) / 2
        self._last_update_time = datetime.now(timezone.utc)
        
        # Search for contracts
        self._find_straddle_instruments()
    
    def on_trade_tick(self, tick: TradeTick) -> None:
        """Handle incoming trade tick data."""
        self._trade_tick_count += 1
        
        # Update state - use trade price as current price
        self._current_price = float(tick.price)
        self._last_update_time = datetime.now(timezone.utc)
        
        # Search for contracts
        self._find_straddle_instruments()
    
    def on_bar(self, bar: Bar) -> None:
        """Handle incoming bar data."""
        self._bar_count += 1
        
        # Update state with close price
        self._current_price = float(bar.close)
        self._last_update_time = datetime.now(timezone.utc)
        
        # Search for contracts
        self._find_straddle_instruments()
    
    def on_data(self, data) -> None:
        """Handle custom data."""
        self._data_count += 1
        self._log_strategy(f"RECEIVED Custom Data #{self._data_count}", "DEBUG")
    
    def on_instrument(self, instrument: Instrument) -> None:
        """Handle instrument updates."""
        # Check if this is a requested option
        if isinstance(instrument, OptionInstrument):
             self._log_strategy(f"Received Option Instrument definition: {instrument.id} (Strike: {instrument.strike_price})", "DEBUG")
        else:
             if instrument.id == self._instrument_id:
                  self._instrument = instrument
                  self._log_strategy(f"RECEIVED Underlying Instrument: {instrument.id}")

    def on_reset(self) -> None:
        """Reset strategy state."""
        self._log_strategy("Strategy RESET called")
        self._current_price = None
        self._current_call = None
        self._current_put = None
        self._quote_tick_count = 0
        self._trade_tick_count = 0
        self._bar_count = 0
        self._data_count = 0


# Factory function for creating strategy with default config
def create_spx_0dte_strategy(
    instrument_id: str = "SPX.CBOE",
    use_bars: bool = True,
    order_id_tag: str = "SPX0DTE001",
    strike_offset: int = 0,
    days_to_expiry: int = 0,
    refresh_interval_seconds: int = 60
) -> Spx0DteStraddleStrategy:
    """
    Factory function to create an SPX 0DTE Straddle Strategy.
    """
    config = Spx0DteStraddleConfig(
        instrument_id=instrument_id,
        use_bars=use_bars,
        order_id_tag=order_id_tag,
        strike_offset=strike_offset,
        days_to_expiry=days_to_expiry,
        refresh_interval_seconds=refresh_interval_seconds
    )
    return Spx0DteStraddleStrategy(config=config)
