"""
SPX 0DTE Opening Straddle Strategy (Premium-Based Selection)

This strategy implements a 0DTE (Zero Days to Expiration) straddle strategy
for the SPX index using NautilusTrader framework.

Key Features:
1. Subscribes to SPX.CBOE index price data
2. Uses Premium-Based Selection instead of ATM strike offset
3. Implements Sliding Window for efficient market data subscriptions
4. Dynamic selection of Call/Put contracts based on target premium
5. Provides comprehensive logging for diagnostics

Premium Selection Logic:
- Monitors option contracts within window_range_strikes from current SPX price
- Selects Call and Put with ask_price closest to target_premium
- Re-centers subscription window when price moves by hysteresis_points

Note on IB Index Data:
- IB transmits index data via reqMktData with Last Price only
- Size values may be invalid/huge for indices
- This strategy uses Bar data (5-second) as primary source for stability
"""

import logging
from decimal import Decimal
from typing import Optional, List, Set, Dict, Tuple
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
    Configuration for SPX 0DTE Straddle Strategy (Premium-Based).
    
    Attributes:
        instrument_id: The SPX instrument identifier (e.g., "SPX.CBOE")
        use_bars: If True, subscribe to 5-second bars instead of ticks
        bar_interval_seconds: Bar interval in seconds
        order_id_tag: Tag for orders
        days_to_expiry: Days to expiry (default 0 for 0DTE)
        refresh_interval_seconds: How often to refresh contract search
        
        Premium-Based Selection Parameters:
        target_premium: Target Ask price for Call and Put selection (default: 2.0)
        window_range_strikes: Number of strikes in each direction from SPX price (default: 20)
        hysteresis_points: SPX price change threshold to trigger window re-centering (default: 7.0)
    """
    instrument_id: str = "SPX.CBOE"
    use_bars: bool = True  # Prefer bars for index data stability
    bar_interval_seconds: int = 5
    order_id_tag: str = "SPX0DTE"
    days_to_expiry: int = 0
    refresh_interval_seconds: int = 60
    
    # Premium-Based Selection Parameters
    target_premium: float = 2.0
    window_range_strikes: int = 20
    hysteresis_points: float = 7.0


class Spx0DteStraddleStrategy(Strategy):
    """
    SPX 0DTE Opening Straddle Strategy with Premium-Based Selection.
    
    This strategy:
    1. Subscribes to SPX index price data
    2. Maintains a sliding window of option subscriptions around current price
    3. Selects Call/Put contracts based on target premium price
    4. Updates selection dynamically on each quote tick
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
        
        # Contract Selection State (Premium-Based)
        self._current_call: Optional[OptionInstrument] = None
        self._current_put: Optional[OptionInstrument] = None
        self._current_call_ask: Optional[float] = None
        self._current_put_ask: Optional[float] = None
        self._last_contract_search_time: Optional[datetime] = None
        
        # Sliding Window State
        self._anchor_price: Optional[float] = None  # Center of subscription window
        self._subscribed_option_ids: Set[InstrumentId] = set()  # Currently subscribed options
        self._option_quotes: Dict[InstrumentId, QuoteTick] = {}  # Latest quotes for options
        
        # Strategy log for UI display
        self._strategy_logs: List[str] = []
        self._max_logs = 200
        
        # Tick/bar counters for diagnostics
        self._quote_tick_count = 0
        self._trade_tick_count = 0
        self._bar_count = 0
        self._data_count = 0
        self._option_quote_count = 0
    
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
            # Premium-Based Selection Data
            "current_call_id": str(self._current_call.id) if self._current_call else None,
            "current_put_id": str(self._current_put.id) if self._current_put else None,
            "current_call_verified": self._check_instrument_verified(self._current_call),
            "current_put_verified": self._check_instrument_verified(self._current_put),
            "current_call_ask": self._current_call_ask,
            "current_put_ask": self._current_put_ask,
            "target_premium": self.config.target_premium,
            # Sliding Window Data
            "anchor_price": self._anchor_price,
            "active_subscriptions": len(self._subscribed_option_ids),
            "option_quotes_cached": len(self._option_quotes),
            "option_quote_count": self._option_quote_count,
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
        self._log_strategy("SPX 0DTE Straddle Strategy STARTING (Premium-Based)")
        self._log_strategy(f"Instrument ID: {self._instrument_id}")
        self._log_strategy(f"Use Bars: {self.config.use_bars}")
        self._log_strategy(f"Config: Target Premium=${self.config.target_premium:.2f}, "
                          f"Window={self.config.window_range_strikes} strikes, "
                          f"Hysteresis={self.config.hysteresis_points} pts")
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
            
        self._log_strategy("Strategy startup complete. Waiting for data...")
    
    def _calculate_window_strikes(self, center_price: float) -> Set[float]:
        """
        Calculate the set of strikes within the window range.
        
        SPX uses 5-point strike increments for most strikes.
        """
        strikes = set()
        # Round to nearest 5
        base_strike = round(center_price / 5) * 5
        
        for offset in range(-self.config.window_range_strikes, self.config.window_range_strikes + 1):
            strike = base_strike + (offset * 5)
            if strike > 0:  # Ensure valid strike
                strikes.add(strike)
        
        return strikes
    
    def _find_options_for_strikes(self, target_strikes: Set[float], target_date) -> List[OptionInstrument]:
        """
        Find option instruments matching the target strikes and expiry.
        Filters for SPXW (weekly/daily) options only.
        """
        matching_options = []
        
        for inst in self.cache.instruments():
            if not isinstance(inst, OptionInstrument):
                continue
            
            # Check underlying
            if inst.underlying_id != self._instrument_id and (
                not self._instrument or inst.underlying_id != self._instrument.id
            ):
                continue
            
            # Filter for SPXW trading class
            t_class = getattr(inst, "trading_class", None)
            if not t_class and hasattr(inst, "info"):
                t_class = inst.info.get("trading_class") or inst.info.get("tradingClass")
            
            if t_class != "SPXW":
                continue
            
            # Check expiry date
            if inst.expiry_date:
                inst_date = inst.expiry_date.date() if isinstance(inst.expiry_date, datetime) else inst.expiry_date
                if inst_date != target_date:
                    continue
            else:
                continue
            
            # Check if strike is in our window
            strike_value = float(inst.strike_price)
            if strike_value in target_strikes:
                matching_options.append(inst)
        
        return matching_options
    
    def _update_subscription_window(self) -> None:
        """
        Update the subscription window based on current SPX price.
        Implements differential subscription updates to minimize API calls.
        """
        if self._current_price is None:
            return
        
        # Check if we need to re-center the window
        if self._anchor_price is not None:
            price_change = abs(self._current_price - self._anchor_price)
            if price_change < self.config.hysteresis_points:
                return  # No re-centering needed
        
        # Re-center the window
        old_anchor = self._anchor_price
        self._anchor_price = self._current_price
        self._log_strategy(f"Window re-centered. New anchor: {self._anchor_price:.2f}. Subscriptions updated.")
        
        # Calculate target expiry (0DTE means today)
        today = datetime.now(timezone.utc).date()
        if self.config.days_to_expiry == 0:
            target_date = today
        else:
            target_date = today + timedelta(days=self.config.days_to_expiry)
        
        # Calculate new window strikes
        target_strikes = self._calculate_window_strikes(self._current_price)
        
        # Find options for these strikes
        matching_options = self._find_options_for_strikes(target_strikes, target_date)
        
        if not matching_options:
            self._log_strategy(f"No options found matching criteria. Strikes: {len(target_strikes)}, Date: {target_date}", "DEBUG")
            return
        
        # Determine which subscriptions to add/remove
        new_option_ids: Set[InstrumentId] = {opt.id for opt in matching_options}
        
        # Calculate differential
        to_subscribe = new_option_ids - self._subscribed_option_ids
        to_unsubscribe = self._subscribed_option_ids - new_option_ids
        
        # Unsubscribe from options outside window
        for opt_id in to_unsubscribe:
            try:
                self.unsubscribe_quote_ticks(opt_id)
                # Clean up quote cache
                if opt_id in self._option_quotes:
                    del self._option_quotes[opt_id]
            except Exception as e:
                self._log_strategy(f"Failed to unsubscribe from {opt_id}: {e}", "DEBUG")
        
        # Subscribe to new options in window
        for opt_id in to_subscribe:
            try:
                self.subscribe_quote_ticks(opt_id)
            except Exception as e:
                self._log_strategy(f"Failed to subscribe to {opt_id}: {e}", "DEBUG")
        
        # Update tracking set
        self._subscribed_option_ids = new_option_ids
        
        self._log_strategy(
            f"Subscriptions: +{len(to_subscribe)} new, -{len(to_unsubscribe)} removed, "
            f"Total: {len(self._subscribed_option_ids)} active",
            "DEBUG"
        )
    
    def _find_straddle_instruments(self) -> None:
        """
        Find best matching Call and Put contracts based on target premium.
        
        Selection criteria:
        - Must be subscribed (in sliding window)
        - Must have valid ask_price > 0
        - Must be verified in cache
        - Choose option with ask_price closest to target_premium
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
        
        # Collect candidates from subscribed options with valid quotes
        call_candidates: List[Tuple[OptionInstrument, float]] = []
        put_candidates: List[Tuple[OptionInstrument, float]] = []
        
        for opt_id in self._subscribed_option_ids:
            # Get instrument from cache
            inst = self.cache.instrument(opt_id)
            if not isinstance(inst, OptionInstrument):
                continue
            
            # Get latest quote
            quote = self._option_quotes.get(opt_id)
            if not quote:
                continue
            
            ask_price = float(quote.ask_price)
            
            # Filter: ask_price must be positive
            if ask_price <= 0:
                continue
            
            # Categorize by option type
            if inst.option_type == OptionType.CALL:
                call_candidates.append((inst, ask_price))
            elif inst.option_type == OptionType.PUT:
                put_candidates.append((inst, ask_price))
        
        # Select best Call (closest to target_premium)
        best_call: Optional[OptionInstrument] = None
        best_call_ask: Optional[float] = None
        best_call_diff = float('inf')
        
        for inst, ask_price in call_candidates:
            diff = abs(ask_price - self.config.target_premium)
            if diff < best_call_diff:
                best_call_diff = diff
                best_call = inst
                best_call_ask = ask_price
        
        # Select best Put (closest to target_premium)
        best_put: Optional[OptionInstrument] = None
        best_put_ask: Optional[float] = None
        best_put_diff = float('inf')
        
        for inst, ask_price in put_candidates:
            diff = abs(ask_price - self.config.target_premium)
            if diff < best_put_diff:
                best_put_diff = diff
                best_put = inst
                best_put_ask = ask_price
        
        # Check if selection changed
        selection_changed = False
        
        if best_call and (self._current_call != best_call or self._current_call_ask != best_call_ask):
            selection_changed = True
        
        if best_put and (self._current_put != best_put or self._current_put_ask != best_put_ask):
            selection_changed = True
        
        if selection_changed and best_call and best_put:
            self._current_call = best_call
            self._current_put = best_put
            self._current_call_ask = best_call_ask
            self._current_put_ask = best_put_ask
            
            # Log the new selection
            self._log_strategy(
                f"Selection updated: Call {best_call.id} (Ask: ${best_call_ask:.2f}), "
                f"Put {best_put.id} (Ask: ${best_put_ask:.2f})"
            )
    
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
        self._log_strategy(f"Active subscriptions at stop: {len(self._subscribed_option_ids)}")
        self._log_strategy("="*60)
        
        # Unsubscribe from all option quotes
        for opt_id in list(self._subscribed_option_ids):
            try:
                self.unsubscribe_quote_ticks(opt_id)
            except Exception:
                pass
        
        self._subscribed_option_ids.clear()
        self._option_quotes.clear()
        
        # Unsubscribe from index data
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
        # Check if this is an option quote or index quote
        if tick.instrument_id in self._subscribed_option_ids:
            # This is an option quote - store it
            self._option_quotes[tick.instrument_id] = tick
            self._option_quote_count += 1
            
            # Trigger premium-based selection update
            self._find_straddle_instruments()
        elif tick.instrument_id == self._instrument_id:
            # This is the SPX index quote
            self._quote_tick_count += 1
            
            # Extract prices
            bid_price = float(tick.bid_price)
            ask_price = float(tick.ask_price)
            
            # Update state
            self._last_bid = bid_price
            self._last_ask = ask_price
            self._current_price = (bid_price + ask_price) / 2
            self._last_update_time = datetime.now(timezone.utc)
            
            # Update subscription window if needed
            self._update_subscription_window()
            
            # Search for contracts
            self._find_straddle_instruments()
    
    def on_trade_tick(self, tick: TradeTick) -> None:
        """Handle incoming trade tick data."""
        self._trade_tick_count += 1
        
        # Update state - use trade price as current price
        self._current_price = float(tick.price)
        self._last_update_time = datetime.now(timezone.utc)
        
        # Update subscription window if needed
        self._update_subscription_window()
        
        # Search for contracts
        self._find_straddle_instruments()
    
    def on_bar(self, bar: Bar) -> None:
        """Handle incoming bar data."""
        self._bar_count += 1
        
        # Update state with close price
        self._current_price = float(bar.close)
        self._last_update_time = datetime.now(timezone.utc)
        
        # Update subscription window if needed
        self._update_subscription_window()
        
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
        self._current_call_ask = None
        self._current_put_ask = None
        self._anchor_price = None
        self._subscribed_option_ids.clear()
        self._option_quotes.clear()
        self._quote_tick_count = 0
        self._trade_tick_count = 0
        self._bar_count = 0
        self._data_count = 0
        self._option_quote_count = 0


# Factory function for creating strategy with default config
def create_spx_0dte_strategy(
    instrument_id: str = "SPX.CBOE",
    use_bars: bool = True,
    order_id_tag: str = "SPX0DTE001",
    days_to_expiry: int = 0,
    refresh_interval_seconds: int = 60,
    target_premium: float = 2.0,
    window_range_strikes: int = 20,
    hysteresis_points: float = 7.0,
) -> Spx0DteStraddleStrategy:
    """
    Factory function to create an SPX 0DTE Straddle Strategy.
    """
    config = Spx0DteStraddleConfig(
        instrument_id=instrument_id,
        use_bars=use_bars,
        order_id_tag=order_id_tag,
        days_to_expiry=days_to_expiry,
        refresh_interval_seconds=refresh_interval_seconds,
        target_premium=target_premium,
        window_range_strikes=window_range_strikes,
        hysteresis_points=hysteresis_points,
    )
    return Spx0DteStraddleStrategy(config=config)
