"""
SPXBaseStrategy - Base class for all SPX-related strategies

Provides:
- Unified SPX instrument subscription with fallback mechanism
- SPX price tracking and tick handling
- Option search by premium (target price)
- State persistence for SPX subscription status
- Abstract methods for strategy-specific SPX logic
- Proper resource cleanup on stop
"""

from abc import abstractmethod
from typing import Dict, Any, Optional, List, Callable
from datetime import datetime, time, timedelta
import pytz
from decimal import Decimal
import uuid


from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.enums import OptionKind, PositionSide
from nautilus_trader.model.position import Position

from .base import BaseStrategy


class SPXBaseStrategy(BaseStrategy):
    """
    Base class for all SPX strategies.
    
    Handles SPX subscription, price tracking, and provides abstract methods
    for strategy-specific SPX logic.
    
    All SPX strategies should inherit from this class and implement:
    - on_spx_ready(): Called when SPX subscription is ready
    - on_spx_tick(tick): Called for each SPX quote tick
    """
    
    # Constants - avoid duplication of instrument ID
    SPX_INSTRUMENT_ID = InstrumentId.from_str("^SPX.CBOE")
    
    # Default timeout for option selection (can be overridden in derived strategies)
    # IMPORTANT: In live trading, increase this value if IB is slow (e.g., market open)
    DEFAULT_SELECTION_DELAY_SECONDS = 10.0
    
    def __init__(self, config, integration_manager=None, persistence_manager=None):
        """
        Initialize SPXBaseStrategy.
        
        Args:
            config: Strategy configuration
            integration_manager: Optional integration manager
            persistence_manager: Optional persistence manager
        """
        super().__init__(config, integration_manager, persistence_manager)
        
        # SPX instrument tracking
        self.spx_instrument_id = self.SPX_INSTRUMENT_ID
        self.spx_instrument: Instrument = None
        self.current_spx_price = 0.0
        self.spx_subscribed = False
        
        # SPX bid/ask tracking - store last valid values
        self.last_spx_bid = 0.0
        self.last_spx_ask = 0.0
        
        # Opening Range Parameters
        params = config.parameters
        self.opening_range_minutes = int(params.get("opening_range_minutes", params.get("window_minutes", 15)))
        timezone_str = params.get("timezone", "US/Eastern")
        self.tz = pytz.timezone(timezone_str)
        self.market_open_time = time(9, 30)
        
        # Opening Range State
        self.daily_high: Optional[float] = None
        self.daily_low: Optional[float] = None
        self.or_high: Optional[float] = None
        self.or_low: Optional[float] = None
        self.range_calculated: bool = False
        self.current_trading_day = None
        
        # Minute Emulation State
        self._last_minute_idx: int = -1
        self._last_tick_price: Optional[float] = None
        self._range_tracking_started: bool = False  # Flag to log once when tracking begins
        
        # Dictionary for parallel option premium searches
        # Key: search_id (UUID), Value: search state dict
        self._premium_searches: Dict[str, Dict[str, Any]] = {}
        
        self.logger.info(
            f"SPXBaseStrategy initialized | OR Window: {self.opening_range_minutes}m | "
            f"TZ: {timezone_str}"
        )
    
    # =========================================================================
    # SPX SUBSCRIPTION WITH FALLBACK MECHANISM
    # =========================================================================
    
    def on_start_safe(self):
        """
        Called after instrument is ready and data subscribed.
        Initiates SPX subscription.
        """
        self.logger.info("SPXBaseStrategy starting - subscribing to SPX")
        self._subscribe_to_spx()
    
    def _subscribe_to_spx(self):
        """
        Subscribe to SPX with fallback logic.
        
        Pattern:
        1. Check if SPX is in cache
        2. If yes: subscribe immediately
        3. If no: request from IB and poll for availability
        """
        # Check if SPX instrument is already in cache
        self.spx_instrument = self.cache.instrument(self.spx_instrument_id)
        
        if self.spx_instrument:
            # SPX already in cache - subscribe immediately
            self.logger.info(f"SPX instrument found in cache: {self.spx_instrument_id}")
            self.subscribe_quote_ticks(self.spx_instrument_id)
            self.spx_subscribed = True
            
            # Notify strategy that SPX is ready
            try:
                self.on_spx_ready()
            except Exception as e:
                self.on_unexpected_error(e)
        else:
            # SPX not in cache - request from Interactive Brokers
            self.logger.info(f"SPX instrument not in cache, requesting from IB: {self.spx_instrument_id}")
            
            self.request_instruments(
                venue=Venue("InteractiveBrokers"),
                params={
                    "ib_contracts": [
                        {
                            "secType": "IND",
                            "symbol": "SPX",
                            "exchange": "CBOE",
                            "currency": "USD"
                        }
                    ]
                }
            )
            
            # BACKTEST-SAFE: Use clock.set_time_alert() instead of asyncio for polling
            # This ensures polling uses simulated time in backtest mode, not wall-clock time
            self.clock.set_time_alert(
                name=f"{self.id}_spx_poll",
                alert_time=self.clock.utc_now() + timedelta(seconds=1),
                callback=self._poll_spx_availability,
            )
            self._spx_poll_attempt = 1
    
    def _poll_spx_availability(self, event):
        """
        Backtest-safe polling method using clock.set_time_alert().
        
        Uses simulated time (data time) rather than wall-clock time,
        ensuring correct behavior in both live and backtest environments.
        
        Args:
            event: Timer event from clock.set_time_alert()
        """
        # Already subscribed (via on_instrument) - stop polling
        if self.spx_subscribed:
            return
        
        attempt = self._spx_poll_attempt
        
        # Check if SPX is now in cache
        self.spx_instrument = self.cache.instrument(self.spx_instrument_id)
        
        if self.spx_instrument:
            self.logger.info(f"SPX instrument found via polling (attempt {attempt}), subscribing...")
            
            # Subscribe to quote ticks
            self.subscribe_quote_ticks(self.spx_instrument_id)
            self.spx_subscribed = True
            
            # Notify strategy that SPX is ready
            try:
                self.on_spx_ready()
            except Exception as e:
                self.on_unexpected_error(e)
            return
        
        # If not found and attempts not exhausted (30 attempts = 30 simulated seconds)
        if attempt < 30:
            self._spx_poll_attempt = attempt + 1
            
            # Schedule next poll using simulated time (not wall-clock!)
            self.clock.set_time_alert(
                name=f"{self.id}_spx_poll",
                alert_time=self.clock.utc_now() + timedelta(seconds=1),
                callback=self._poll_spx_availability,
            )
        else:
            # Timeout - SPX not available after 30 simulated seconds
            self.logger.error(
                f"Timeout waiting for SPX instrument {self.spx_instrument_id} from IB. "
                "Strategy may not function correctly."
            )
    
    # =========================================================================
    # SPX TICK HANDLING
    # =========================================================================
    
    def on_quote_tick_safe(self, tick: QuoteTick):
        """
        Standardizes SPX price tracking and OR calculation.
        """
        # Check if this is an SPX tick
        if tick.instrument_id == self.spx_instrument_id:
            # Get current bid/ask from tick
            bid = tick.bid_price.as_double()
            ask = tick.ask_price.as_double()
            
            # Use previous valid values if current values are zero
            if bid <= 0:
                bid = self.last_spx_bid
            else:
                self.last_spx_bid = bid  # Store valid bid
            
            if ask <= 0:
                ask = self.last_spx_ask
            else:
                self.last_spx_ask = ask  # Store valid ask
            
            # Calculate mid price from bid/ask
            if bid > 0 and ask > 0:
                self.current_spx_price = (bid + ask) / 2
            elif bid > 0:
                self.current_spx_price = bid
            elif ask > 0:
                self.current_spx_price = ask
            
            # Core SPX Processing (ported from SPX_15Min_Range)
            self._process_spx_tick_unified(tick)
            
            # Call strategy-specific SPX tick handler
            self.on_spx_tick(tick)
        else:
            # Not an SPX tick (e.g., an option or spread)
            pass

    def _process_spx_tick_unified(self, tick: QuoteTick):
        """
        Ported from SPX_15Min_Range: Unified tick processing for all SPX strategies.
        Handles daily reset, minute-close emulation, and range calculation.
        """
        utc_now = self.clock.utc_now()
        et_now = utc_now.astimezone(self.tz)
        current_date = et_now.date()
        current_time = et_now.time()
        price = self.current_spx_price

        if price <= 0:
            return

        # 1. Reset state on new trading day
        if self.current_trading_day != current_date:
            self._reset_daily_state(current_date)
            self._last_minute_idx = -1

        # 2. Minute change logic (Candle Close Emulation)
        current_minute_idx = current_time.hour * 60 + current_time.minute
        
        if self._last_minute_idx != -1 and current_minute_idx != self._last_minute_idx:
            # New minute started - previous minute closed
            if self._last_tick_price:
                self.on_minute_closed(self._last_tick_price)
        
        self._last_minute_idx = current_minute_idx
        self._last_tick_price = price

        # 3. Range window calculation (09:30 + opening_range_minutes)
        end_minute = self.market_open_time.minute + self.opening_range_minutes
        end_hour = self.market_open_time.hour + (end_minute // 60)
        end_minute = end_minute % 60
        range_end_time = time(end_hour, end_minute, 0)

        # 4. Update daily high/low throughout the day (from market open)
        if current_time >= self.market_open_time:
            if not self.daily_high or price > self.daily_high:
                self.daily_high = price
            if not self.daily_low or price < self.daily_low:
                self.daily_low = price

        # 5. Range formation period (logic for locking OR)
        if self.market_open_time <= current_time < range_end_time:
            # Log once when entering range tracking window
            if not self._range_tracking_started:
                self._range_tracking_started = True
                self.logger.info(
                    f"🕘 OPENING RANGE TRACKING STARTED | Window: {self.market_open_time.strftime('%H:%M')}-{range_end_time.strftime('%H:%M')} ET",
                    extra={"extra": {
                        "event_type": "range_tracking_started",
                        "current_time_et": str(current_time),
                        "range_start": str(self.market_open_time),
                        "range_end": str(range_end_time),
                        "spx_price": price
                    }}
                )
                self._notify(
                    f"🕘 OPENING RANGE TRACKING STARTED | Window: {self.market_open_time.strftime('%H:%M')}-{range_end_time.strftime('%H:%M')} ET"
                )
            self.range_calculated = False

        # 5. Lock in range after window period
        elif current_time >= range_end_time and not self.range_calculated:
            if self.daily_high and self.daily_low:
                self.or_high = self.daily_high
                self.or_low = self.daily_low
                self.range_calculated = True
                self.logger.info(
                    f"📈 RANGE LOCKED ({self.opening_range_minutes}m) | High={self.or_high:.2f} Low={self.or_low:.2f} Width={self.or_high - self.or_low:.2f}",
                    extra={"extra": {
                        "event_type": "range_locked",
                        "range_minutes": self.opening_range_minutes,
                        "or_high": self.or_high,
                        "or_low": self.or_low,
                        "range_width": self.or_high - self.or_low,
                        "lock_time_et": str(current_time)
                    }}
                )
                self.save_state()
                self._notify(
                    f"📈 RANGE LOCKED ({self.opening_range_minutes}m) | High={self.or_high:.2f} Low={self.or_low:.2f} Width={self.or_high - self.or_low:.2f}",
                )
            else:
                self.logger.error(
                    f"❌ RANGE LOCK FAILED | Insufficient data at {current_time}",
                    extra={"extra": {
                        "event_type": "range_lock_failed",
                        "lock_time_et": str(current_time),
                        "reason": "insufficient_data",
                        "daily_high": self.daily_high,
                        "daily_low": self.daily_low
                    }}
                )
                self.range_calculated = True

    def _reset_daily_state(self, current_date):
        """Reset all daily tracking state."""
        self.logger.info(f"Resetting daily SPX state for {current_date}")
        self.current_trading_day = current_date
        self.daily_high = None
        self.daily_low = None
        self.or_high = None
        self.or_low = None
        self.range_calculated = False
        self._range_tracking_started = False  # Reset so log fires again next day
        self._last_tick_price = None
    
    # =========================================================================
    # LIFECYCLE MANAGEMENT
    # =========================================================================
    
    def on_stop_safe(self):
        """
        Called when strategy is stopped.
        Unsubscribes from SPX data to free IB market data slots.
        """
        # Unsubscribe from SPX if subscribed
        if self.spx_subscribed:
            try:
                self.logger.info(f"Unsubscribing from SPX: {self.spx_instrument_id}")
                self.unsubscribe_quote_ticks(self.spx_instrument_id)
                self.spx_subscribed = False
            except Exception as e:
                self.logger.error(f"Failed to unsubscribe from SPX: {e}")
        
        # Cancel ALL active searches on stop
        for search_id in list(self._premium_searches.keys()):
            self.cancel_premium_search(search_id)

        # Call parent cleanup
        super().on_stop_safe()
    
    # =========================================================================
    # STATE PERSISTENCE
    # =========================================================================
    
    def get_state(self) -> Dict[str, Any]:
        """
        Return strategy-specific state to persist.
        """
        state = super().get_state()
        state.update({
            "current_spx_price": self.current_spx_price,
            "spx_subscribed": self.spx_subscribed,
            "last_spx_bid": self.last_spx_bid,
            "last_spx_ask": self.last_spx_ask,
            "or_high": self.or_high,
            "or_low": self.or_low,
            "range_calculated": self.range_calculated,
            "current_trading_day": self.current_trading_day.isoformat() if self.current_trading_day else None,
            "daily_high": self.daily_high,
            "daily_low": self.daily_low,
        })
        return state
    
    def set_state(self, state: Dict[str, Any]):
        """
        Restore strategy-specific state.
        """
        super().set_state(state)
        self.current_spx_price = state.get("current_spx_price", 0.0)
        self.spx_subscribed = state.get("spx_subscribed", False)
        self.last_spx_bid = state.get("last_spx_bid", 0.0)
        self.last_spx_ask = state.get("last_spx_ask", 0.0)
        self.or_high = state.get("or_high")
        self.or_low = state.get("or_low")
        self.range_calculated = state.get("range_calculated", False)
        
        day_str = state.get("current_trading_day")
        if day_str:
            self.current_trading_day = datetime.fromisoformat(day_str).date()
        
        self.daily_high = state.get("daily_high")
        self.daily_low = state.get("daily_low")
        
        self.logger.info(
            f"SPX state restored: price={self.current_spx_price:.2f}, "
            f"OR={self.or_high}/{self.or_low}"
        )
    
    # =========================================================================
    # INSTRUMENT DISCOVERY
    # =========================================================================
    
    def request_option_chain(self, expiry_date: str):
        """
        Request the entire SPX option chain for a specific expiry date.
        Uses the 'build_options_chain' capability of the IB provider.
        
        Args:
            expiry_date: Expiry date in 'YYYYMMDD' format
        """
        self.logger.info(f"Requesting SPX option chain for expiry: {expiry_date}")
        
        try:
            self.request_instruments(
                venue=Venue("IB"),
                params={
                    "ib_contracts": (
                        {
                            "secType": "IND",
                            "symbol": "SPX",
                            "exchange": "CBOE",
                            "build_options_chain": True,
                            "lastTradeDateOrContractMonth": expiry_date,
                        },
                    )
                }
            )
        except Exception as e:
            self.logger.error(f"Failed to request SPX option chain for {expiry_date}: {e}")

    
    # =========================================================================
    # OPTION SEARCH BY PREMIUM
    # =========================================================================
    
    def find_option_by_premium(
        self,
        target_premium: float,
        option_kind: OptionKind,
        expiry_date: Optional[str] = None,
        strike_range: int = 15,
        strike_step: int = 5,
        max_spread: Optional[float] = None,
        selection_delay_seconds: float = 10.0,
        callback: Optional[Callable[[Optional[Instrument], Optional[Dict]], None]] = None
    ) -> Optional[str]:
        """
        Search for SPX options by target premium (option price).
        
        This method requests multiple strikes around ATM, collects quotes,
        and finds the option with price closest to target premium.
        
        Supports parallel searches - each call creates a new isolated search
        identified by a unique ID. This allows searching for multiple options
        simultaneously (e.g., for Iron Condor strategies).
        
        Args:
            target_premium: Target option price (e.g., 4.0 for $4 option)
            option_kind: OptionKind.CALL or OptionKind.PUT
            expiry_date: Expiry date in YYYYMMDD format, default today (0DTE)
            strike_range: Number of strikes to request (default 7)
            strike_step: Step between strikes in points (default 5)
            max_spread: Maximum allowed bid-ask spread, None = no filter
            selection_delay_seconds: Delay before selecting best option (default 2.0)
            callback: Function to call with (search_id, selected_option, option_data) or (search_id, None, None) if failed
        
        Returns:
            search_id: Unique ID for this search (can be used to cancel)
            
        Example:
            # For Iron Condor - search for multiple options in parallel
            call_search_id = self.find_option_by_premium(
                target_premium=4.0,
                option_kind=OptionKind.CALL,
                max_spread=0.20,
                callback=self._on_call_found
            )
            put_search_id = self.find_option_by_premium(
                target_premium=4.0,
                option_kind=OptionKind.PUT,
                max_spread=0.20,
                callback=self._on_put_found
            )
        """
        if self.current_spx_price == 0:
            self.logger.error("Cannot search for options: SPX price not available")
            if callback:
                callback(None, None, None)
            return None
        
        # Generate unique ID for this search
        search_id = str(uuid.uuid4())
        
        # Initialize search state in dictionary
        self._premium_searches[search_id] = {
            'search_id': search_id,
            'target_premium': target_premium,
            'option_kind': option_kind,
            'max_spread': max_spread,
            'callback': callback,
            'received_options': [],
            'subscribed_instrument_ids': [],  # Track subscribed options for cleanup
            'active': True
        }
        
        # Calculate ATM strike (round to nearest 5)
        atm_strike = round(self.current_spx_price / strike_step) * strike_step
        
        # Calculate strike range
        half_range = strike_range // 2
        if option_kind == OptionKind.CALL:
            # For calls: request ATM and OTM strikes
            strikes = [atm_strike + (i * strike_step) for i in range(strike_range)]
        else:
            # For puts: request ATM and ITM strikes
            strikes = [atm_strike - (i * strike_step) for i in range(strike_range)]
        
        # Get expiry date (default today for 0DTE)
        if expiry_date is None:
            expiry_date = self.clock.utc_now().date().strftime("%Y%m%d")
        
        right = "C" if option_kind == OptionKind.CALL else "P"
        
        self.logger.info(
            f"🔍 Searching for {option_kind.name} option with premium ~${target_premium:.2f}\n"
            f"   ATM Strike: ${atm_strike:.0f}, Strikes: {strikes}\n"
            f"   Expiry: {expiry_date}, Max Spread: ${max_spread if max_spread else 'N/A'}"
        )
        
        # Build option contracts to request
        contracts = []
        for strike in strikes:
            contracts.append({
                "secType": "OPT",
                "symbol": "SPX",
                "tradingClass": "SPXW",
                "exchange": "CBOE",
                "currency": "USD",
                "lastTradeDateOrContractMonth": expiry_date,
                "strike": float(strike),
                "right": right,
                "multiplier": "100"
            })
        
        try:
            self.request_instruments(
                venue=Venue("CBOE"),
                params={"ib_contracts": contracts}
            )
            
            self.logger.info(f"✅ Requested {len(contracts)} SPX {option_kind.name} options (search_id: {search_id[:8]}...)")
            
            # Set timer with unique name for this search
            timer_name = f"{self.id}.premium_search.{search_id}"
            
            self.clock.set_time_alert(
                name=timer_name,
                alert_time=self.clock.utc_now() + timedelta(seconds=selection_delay_seconds),
                callback=self._on_premium_search_complete
            )
            
            return search_id
            
        except Exception as e:
            self.logger.error(f"❌ Failed to request options: {e}", exc_info=True)
            # Clean up failed search
            self._premium_searches.pop(search_id, None)
            if callback:
                callback(search_id, None, None)
            return None
    
    def _handle_option_for_premium_search(self, instrument: Instrument):
        """
        Internal handler for options received during premium search.
        Called from on_instrument().
        
        Routes received options to all active searches that match the option type.
        
        Args:
            instrument: Received option instrument
        """
        # Check if this is an option instrument
        if not hasattr(instrument, 'option_kind'):
            return
        
        # Check all active searches
        for search_id, state in self._premium_searches.items():
            if not state.get('active'):
                continue
            
            # Check if this option matches the search criteria
            if instrument.option_kind != state['option_kind']:
                continue
            
            self.logger.debug(f"Received option for search {search_id[:8]}...: {instrument.id}")
            
            # Subscribe to quotes for this option
            self.subscribe_quote_ticks(instrument.id)
            
            # Track subscription for cleanup after search completes
            if instrument.id not in state['subscribed_instrument_ids']:
                state['subscribed_instrument_ids'].append(instrument.id)
            
            # Add to this search's received options
            state['received_options'].append(instrument)
    
    def _on_premium_search_complete(self, timer_event):
        """
        Timer callback to select best option after receiving options.
        Finds option with price closest to target premium.
        
        Extracts search_id from timer name to support parallel searches.
        """
        # Extract search_id from timer name: "{self.id}.premium_search.{search_id}"
        timer_name = timer_event.name if hasattr(timer_event, 'name') else str(timer_event)
        parts = timer_name.rsplit('.', 1)
        if len(parts) < 2:
            self.logger.error(f"Invalid timer name format: {timer_name}")
            return
        
        search_id = parts[-1]
        
        # Get and validate search state
        state = self._premium_searches.get(search_id)
        if not state or not state.get('active'):
            self.logger.warning(f"Search {search_id[:8]}... not found or already completed")
            return
        
        state['active'] = False  # Mark search complete
        
        received_options = state['received_options']
        target_premium = state['target_premium']
        max_spread = state['max_spread']
        callback = state['callback']
        
        self.logger.info(f"🔍 Completing premium search {search_id[:8]}...")
        
        if not received_options:
            self.logger.warning(f"No options received for search {search_id[:8]}...")
            # Clean up and call callback
            self._premium_searches.pop(search_id, None)
            if callback:
                callback(search_id, None, None)
            return
        
        self.logger.info(f"Selecting from {len(received_options)} received options")
        
        # Collect quotes for all options
        option_prices = []
        
        for option in received_options:
            quote = self.cache.quote_tick(option.id)
            
            if not quote:
                continue
            
            bid = quote.bid_price.as_double()
            ask = quote.ask_price.as_double()
            
            if bid <= 0 or ask <= 0:
                continue
            
            mid = (bid + ask) / 2
            spread = ask - bid
            
            # Apply spread filter if specified
            if max_spread is not None and spread > max_spread:
                self.logger.debug(
                    f"  Strike ${float(option.strike_price.as_double()):.0f}: "
                    f"SKIPPED (spread ${spread:.2f} > max ${max_spread:.2f})"
                )
                continue
            
            option_data = {
                'option': option,
                'bid': bid,
                'ask': ask,
                'mid': mid,
                'spread': spread,
                'strike': float(option.strike_price.as_double())
            }
            option_prices.append(option_data)
            
            self.logger.info(
                f"  Strike ${option_data['strike']:.0f}: "
                f"Mid=${mid:.2f}, Spread=${spread:.2f}"
            )
        
        # Get subscribed instruments for cleanup
        subscribed_instrument_ids = state.get('subscribed_instrument_ids', [])
        
        # Clean up search state
        self._premium_searches.pop(search_id, None)
        
        if not option_prices:
            self.logger.warning(f"No valid option quotes for search {search_id[:8]}... after filtering")
            
            # --- CRITICAL FIX: Unsubscribe from ALL options since none were selected ---
            self._unsubscribe_from_options(subscribed_instrument_ids, keep_instrument_ids=None, exclude_search_id=search_id)
            
            if callback:
                callback(search_id, None, None)
            return
        
        # Find option closest to target premium
        best_option_data = min(
            option_prices,
            key=lambda x: abs(x['mid'] - target_premium)
        )
        
        selected_option = best_option_data['option']
        
        # --- CRITICAL FIX: Unsubscribe from all options EXCEPT the selected one ---
        # This prevents IB market data limit errors from accumulating subscriptions
        self._unsubscribe_from_options(subscribed_instrument_ids, keep_instrument_ids=[selected_option.id], exclude_search_id=search_id)
        
        self.logger.info(
            f"✅ Search {search_id[:8]}... selected: Strike ${best_option_data['strike']:.0f}, "
            f"Mid=${best_option_data['mid']:.2f} "
            f"(target: ${target_premium:.2f}), "
            f"Spread=${best_option_data['spread']:.2f}"
        )
        
        # Call callback with search_id and result
        if callback:
            callback(search_id, selected_option, best_option_data)
    
    def _unsubscribe_from_options(
        self, 
        subscribed_instrument_ids: List[InstrumentId], 
        keep_instrument_ids: Optional[List[InstrumentId]] = None,
        exclude_search_id: Optional[str] = None
    ):
        """
        Unsubscribe from option quotes, optionally keeping specific subscriptions.
        
        CRITICAL: This prevents IB market data limit errors by cleaning up
        subscriptions to options we didn't select.
        
        Args:
            subscribed_instrument_ids: List of instrument IDs to unsubscribe from
            keep_instrument_ids: Optional list of IDs to keep subscribed
            exclude_search_id: Optional search_id that is calling this, to check other active searches
        """
        if keep_instrument_ids is None:
            keep_instrument_ids = []
            
        unsubscribed_count = 0
        for instrument_id in subscribed_instrument_ids:
            # Don't unsubscribe from the ones we want to keep
            if instrument_id in keep_instrument_ids:
                continue
                
            # Check if any OTHER active search still needs this instrument
            is_needed_elsewhere = False
            for s_id, s_state in self._premium_searches.items():
                if exclude_search_id and s_id == exclude_search_id:
                    continue
                if not s_state.get('active'):
                    continue
                if instrument_id in s_state.get('subscribed_instrument_ids', []):
                    is_needed_elsewhere = True
                    break
            
            if is_needed_elsewhere:
                continue

            try:
                self.unsubscribe_quote_ticks(instrument_id)
                unsubscribed_count += 1
            except Exception as e:
                self.logger.warning(f"Failed to unsubscribe from {instrument_id}: {e}")
        
        if unsubscribed_count > 0:
            kept_info = f"{len(keep_instrument_ids)} kept" if keep_instrument_ids else "none"
            self.logger.info(
                f"🧹 Cleaned up {unsubscribed_count} option subscriptions ({kept_info})"
            )
    
    def cancel_premium_search(self, search_id: str) -> bool:
        """
        Cancel an active premium search.
        
        Args:
            search_id: The search ID returned by find_option_by_premium()
            
        Returns:
            True if search was cancelled, False if not found
        """
        state = self._premium_searches.pop(search_id, None)
        if state:
            state['active'] = False
            
            # Unsubscribe from all options in this search
            subscribed_ids = state.get('subscribed_instrument_ids', [])
            self._unsubscribe_from_options(subscribed_ids, None, exclude_search_id=search_id)
            
            # Cancel the timer
            timer_name = f"{self.id}.premium_search.{search_id}"
            try:
                self.clock.cancel_timer(timer_name)
            except Exception:
                pass
            
            self.logger.info(f"Cancelled premium search {search_id[:8]}...")
            return True
        return False
    
    def get_active_searches(self) -> List[str]:
        """
        Get list of active search IDs.
        
        Returns:
            List of search_id strings for active searches
        """
        return [
            search_id for search_id, state in self._premium_searches.items()
            if state.get('active')
        ]

    # =========================================================================
    # OPTION SEARCH BY DELTA (using greeks.py Black-Scholes)
    # =========================================================================

    def find_option_by_delta(
        self,
        target_delta: float,
        option_kind: OptionKind,
        expiry_date: Optional[str] = None,
        strike_range: int = 40,
        strike_step: int = 5,
        max_spread: Optional[float] = None,
        selection_delay_seconds: float = 12.0,
        callback: Optional[Callable] = None,
        risk_free_rate: float = 0.05,
        dividend_yield: float = 0.013,
    ) -> Optional[str]:
        """
        Find an SPX option with a specific target delta using Nautilus Greeks.
        
        Args:
            target_delta: Target delta (e.g. -0.25 for put, 0.25 for call)
            option_kind: CALL or PUT
            expiry_date: Date string YYYYMMDD (defaults to 0DTE or next trading day)
            strike_range: [Ignored] - uses all cached options
            strike_step: [Ignored] - uses all cached options
            max_spread: Optional maximum allowed bid-ask spread
            selection_delay_seconds: Seconds to wait for quotes before selection
            callback: Called with (search_id, selected_option, best_stats)
            risk_free_rate: [Ignored] - uses Nautilus engine greeks
            dividend_yield: [Ignored] - uses Nautilus engine greeks
            
        Returns:
            search_id (str) or None on immediate failure.
        """
        search_id = str(uuid.uuid4())
        
        # Determine expiry date
        if expiry_date is None:
            expiry_date = self.clock.utc_now().date().strftime("%Y%m%d")

        self.logger.info(
            f"🔍 Delta Search {search_id[:8]} started | Target Δ={target_delta} | Expiry={expiry_date}"
        )

        # Store search state
        self._premium_searches[search_id] = {
            'search_id': search_id,
            'search_type': 'delta',
            'target_delta': target_delta,
            'option_kind': option_kind,
            'expiry_date': expiry_date,
            'max_spread': max_spread,
            'callback': callback,
            'received_options': [],
            'subscribed_instrument_ids': [],
            'active': True,
        }

        # Filter cached instruments for matching options
        all_instruments = self.cache.instruments()
        match_count = 0
        
        # Year format can be problematic in IB (YYYY vs YY)
        expiry_short = expiry_date[2:] if len(expiry_date) == 8 else expiry_date
        
        # Filter range based on current SPX price to avoid hitting IB ticker limit (usually 100)
        # 1DTE/0DTE SPX is around 6000, 150 points is ~2.5%, very safe for delta targets -0.4 to -0.1
        current_price = self.current_spx_price
        if current_price <= 0:
             # Fallback to cache lookup if live price hasn't arrived yet
             last_trade = self.cache.trade_tick(self.spx_instrument_id)
             if last_trade:
                 current_price = float(last_trade.price)
        
        strike_limit_range = 150.0 # Points
        
        for inst in all_instruments:
            # Must be an option
            if not hasattr(inst, 'option_kind') or inst.option_kind != option_kind:
                continue
            
            # Must match symbol (SPX or SPXW)
            symbol = str(inst.id.symbol)
            if not (symbol.startswith("SPX") or symbol.startswith("SPXW")):
                continue
                
            # Must match expiry
            # We check both the instrument attribute and the ID string
            inst_expiry = ""
            if hasattr(inst, 'expiry'):
                inst_expiry = str(inst.expiry)
            elif hasattr(inst, 'last_trade_date'):
                inst_expiry = str(inst.last_trade_date)
            
            # Broad check for expiry string match
            if expiry_date not in inst_expiry and expiry_short not in symbol:
                continue

            # --- MASS TICKER LIMIT PROTECTION ---
            # Only subscribe to options within a reasonable range of current price
            if current_price > 0:
                strike = float(inst.strike_price.as_double())
                if abs(strike - current_price) > strike_limit_range:
                    continue

            # This is a candidate
            self.subscribe_quote_ticks(inst.id)
            self._premium_searches[search_id]['received_options'].append(inst)
            self._premium_searches[search_id]['subscribed_instrument_ids'].append(inst.id)
            match_count += 1

        if match_count == 0:
            self.logger.warning(
                f"❌ Delta search {search_id[:8]} found NO options matching expiry {expiry_date} in cache"
            )
            self._premium_searches.pop(search_id, None)
            if callback:
                callback(search_id, None, None)
            return None

        self.logger.info(
            f"📡 Delta search {search_id[:8]} found {match_count} candidates in cache for expiry {expiry_date}"
        )

        # Set selection timer
        timer_name = f"{self.id}.premium_search.{search_id}"
        self.clock.set_time_alert(
            name=timer_name,
            alert_time=self.clock.utc_now() + timedelta(seconds=selection_delay_seconds),
            callback=self._on_delta_search_complete
        )

        return search_id

    def _on_delta_search_complete(self, timer_event):
        """
        Processes collected option data and selects the best delta match.
        Uses Nautilus built-in Greeks for maximum accuracy.
        """
        timer_name = timer_event.name if hasattr(timer_event, 'name') else str(timer_event)
        parts = timer_name.rsplit('.', 1)
        if len(parts) < 2:
            self.logger.error(f"Invalid delta search timer: {timer_name}")
            return

        search_id = parts[-1]
        state = self._premium_searches.get(search_id)
        if not state or not state.get('active'):
            return

        state['active'] = False
        received_options = state['received_options']
        target_delta = state['target_delta']
        max_spread = state['max_spread']
        callback = state['callback']
        subscribed_ids = state.get('subscribed_instrument_ids', [])

        # Essential: Remove from active searches
        self._premium_searches.pop(search_id, None)

        if not received_options:
            self.logger.warning(f"Delta search {search_id[:8]}: no options to evaluate")
            self._unsubscribe_from_options(subscribed_ids, keep_instrument_ids=None, exclude_search_id=search_id)
            if callback:
                callback(search_id, None, None)
            return

        self.logger.info(
            f"🔍 Completing delta search {search_id[:8]} | "
            f"{len(received_options)} options to evaluate | Target Δ={target_delta:.3f}"
        )

        abs_target = abs(target_delta)
        candidates = []

        for option in received_options:
            try:
                # Use Nautilus greeks_calculator
                greeks_data = self.greeks.instrument_greeks(option.id)
                if not greeks_data or greeks_data.delta is None:
                    continue

                quote = self.cache.quote_tick(option.id)
                if not quote:
                    continue

                bid = quote.bid_price.as_double()
                ask = quote.ask_price.as_double()
                if bid <= 0 or ask <= 0:
                    continue

                mid = (bid + ask) / 2.0
                spread = ask - bid
                
                if max_spread is not None and spread > max_spread:
                    continue

                strike = float(option.strike_price.as_double())
                delta = float(greeks_data.delta)
                abs_delta = abs(delta)

                candidates.append({
                    'option': option,
                    'delta': delta,
                    'abs_delta': abs_delta,
                    'mid': mid,
                    'spread': spread,
                    'strike': strike,
                    'iv': greeks_data.vol,
                    'bid': bid,
                    'ask': ask,
                })

                self.logger.debug(
                    f"  ${strike:.0f}: Δ={delta:.4f} IV={greeks_data.vol:.2%} "
                    f"Mid=${mid:.2f} Spread=${spread:.2f}"
                )

            except Exception as e:
                self.logger.warning(f"Greeks retrieval failed for {option.id}: {e}")

        if not candidates:
            self.logger.warning(f"Delta search {search_id[:8]}: no valid candidates after evaluation")
            self._unsubscribe_from_options(subscribed_ids, None, exclude_search_id=search_id)
            if callback:
                callback(search_id, None, None)
            return

        # Sort candidates by proximity to target delta
        candidates.sort(key=lambda x: abs(x['abs_delta'] - abs_target))
        
        # Log top 10 closest strikes for transparency
        self.logger.info(f"📊 Top 10 closest strikes for search {search_id[:8]}:")
        for i, c in enumerate(candidates[:10]):
            dist = abs(c['abs_delta'] - abs_target)
            self.logger.info(
                f"  {i+1}. Strike ${c['strike']:.0f} | Δ={c['delta']:.4f} | "
                f"Dist={dist:.4f} | Mid=${c['mid']:.2f} | IV={c['iv']:.2%}"
            )

        # Select option closest to target delta (by absolute value)
        best = candidates[0]
        selected_option = best['option']

        # Unsubscribe from all EXCEPT selected
        self._unsubscribe_from_options(subscribed_ids, keep_instrument_ids=[selected_option.id], exclude_search_id=search_id)


        self.logger.info(
            f"✅ Delta search {search_id[:8]} selected: "
            f"Strike=${best['strike']:.0f} Δ={best['delta']:.4f} "
            f"(target={target_delta:.3f}) IV={best['iv']:.2%} Mid=${best['mid']:.2f}"
        )

    def find_options_by_deltas(
        self,
        target_deltas: List[float],
        option_kind: OptionKind,
        expiry_date: Optional[str] = None,
        selection_delay_seconds: float = 12.0,
        callback: Optional[Callable] = None,
        max_spread: Optional[float] = None,
    ) -> Optional[str]:
        """
        Find multiple SPX options with specific target deltas in parallel.
        
        This method is more efficient than calling find_option_by_delta 
        multiple times sequentially, as it reduces total wait time and 
        ensures consistency.
        
        Args:
            target_deltas: List of target deltas (e.g. [-0.25, -0.14])
            option_kind: CALL or PUT
            expiry_date: Date string YYYYMMDD
            selection_delay_seconds: Seconds to wait for quotes
            callback: Called with (search_id, List[selected_options], List[stats])
            max_spread: Optional maximum allowed bid-ask spread
            
        Returns:
            search_id (str) or None on immediate failure.
        """
        search_id = str(uuid.uuid4())
        
        # Determine expiry date
        if expiry_date is None:
            expiry_date = self.clock.utc_now().date().strftime("%Y%m%d")

        self.logger.info(
            f"🔍 Multi-Delta Search {search_id[:8]} started | Targets={target_deltas} | Expiry={expiry_date}"
        )

        # Store search state
        self._premium_searches[search_id] = {
            'search_id': search_id,
            'search_type': 'multi_delta',
            'target_deltas': target_deltas,
            'option_kind': option_kind,
            'expiry_date': expiry_date,
            'max_spread': max_spread,
            'callback': callback,
            'received_options': [],
            'subscribed_instrument_ids': [],
            'active': True,
        }

        # Filter range based on current SPX price to avoid hitting IB ticker limit
        current_price = self.current_spx_price
        if current_price <= 0:
             # Fallback to cache lookup if live price hasn't arrived yet
             last_trade = self.cache.trade_tick(self.spx_instrument_id)
             if last_trade:
                 current_price = float(last_trade.price)
        
        strike_limit_range = 150.0 # Points
        
        # Filter cached instruments for matching options
        all_instruments = self.cache.instruments()
        match_count = 0
        
        # Year format fallback
        expiry_short = expiry_date[2:] if len(expiry_date) == 8 else expiry_date
        
        for inst in all_instruments:
            # Must be an option
            if not hasattr(inst, 'option_kind') or inst.option_kind != option_kind:
                continue
            
            # Must match symbol (SPX or SPXW)
            symbol = str(inst.id.symbol)
            if not (symbol.startswith("SPX") or symbol.startswith("SPXW")):
                continue
                
            # Must match expiry
            inst_expiry = ""
            if hasattr(inst, 'expiry'):
                inst_expiry = str(inst.expiry)
            elif hasattr(inst, 'last_trade_date'):
                inst_expiry = str(inst.last_trade_date)
            
            if expiry_date not in inst_expiry and expiry_short not in symbol:
                continue

            # --- MASS TICKER LIMIT PROTECTION ---
            if current_price > 0:
                strike = float(inst.strike_price.as_double())
                if abs(strike - current_price) > strike_limit_range:
                    continue

            # This is a candidate
            self.subscribe_quote_ticks(inst.id)
            self._premium_searches[search_id]['received_options'].append(inst)
            self._premium_searches[search_id]['subscribed_instrument_ids'].append(inst.id)
            match_count += 1

        if match_count == 0:
            self.logger.warning(
                f"❌ Multi-delta search {search_id[:8]} found NO options matching expiry {expiry_date} in cache"
            )
            self._premium_searches.pop(search_id, None)
            if callback:
                callback(search_id, [], [])
            return None

        self.logger.info(
            f"📡 Multi-delta search {search_id[:8]} found {match_count} candidates in cache for expiry {expiry_date}"
        )

        # Set selection timer
        timer_name = f"{self.id}.premium_search.{search_id}"
        self.clock.set_time_alert(
            name=timer_name,
            alert_time=self.clock.utc_now() + timedelta(seconds=selection_delay_seconds),
            callback=self._on_multi_delta_search_complete
        )

        return search_id

    def _on_multi_delta_search_complete(self, timer_event):
        """
        Finalizes multi-delta discovery. Selects best match for EACH target delta.
        """
        timer_name = timer_event.name if hasattr(timer_event, 'name') else str(timer_event)
        search_id = timer_name.rsplit('.', 1)[-1]
        
        state = self._premium_searches.get(search_id)
        if not state or not state.get('active'):
            return

        state['active'] = False
        received_options = state['received_options']
        target_deltas = state['target_deltas']
        max_spread = state['max_spread']
        callback = state['callback']
        subscribed_ids = state.get('subscribed_instrument_ids', [])

        # Remove from active searches
        self._premium_searches.pop(search_id, None)

        if not received_options:
            self.logger.warning(f"Multi-delta search {search_id[:8]}: no options to evaluate")
            self._unsubscribe_from_options(subscribed_ids, None, exclude_search_id=search_id)
            if callback:
                callback(search_id, [], [])
            return

        # 1. Evaluate all candidates once to get their Greeks/Prices
        candidates = []
        for option in received_options:
            try:
                greeks_data = self.greeks.instrument_greeks(option.id)
                if not greeks_data or greeks_data.delta is None:
                    continue

                quote = self.cache.quote_tick(option.id)
                if not quote: continue

                bid = quote.bid_price.as_double()
                ask = quote.ask_price.as_double()
                if bid <= 0 or ask <= 0: continue

                mid = (bid + ask) / 2.0
                spread = ask - bid
                if max_spread is not None and spread > max_spread:
                    continue

                candidates.append({
                    'option': option,
                    'delta': float(greeks_data.delta),
                    'abs_delta': abs(float(greeks_data.delta)),
                    'mid': mid,
                    'strike': float(option.strike_price.as_double()),
                    'iv': greeks_data.vol,
                    'bid': bid,
                    'ask': ask,
                    'spread': spread
                })
            except Exception:
                continue

        if not candidates:
            self.logger.warning(f"Multi-delta search {search_id[:8]}: evaluation failed for all options")
            self._unsubscribe_from_options(subscribed_ids, None, exclude_search_id=search_id)
            if callback: callback(search_id, [], [])
            return

        # 2. For each target, find the best match
        results_options = [None] * len(target_deltas)
        results_stats = [None] * len(target_deltas)
        keep_instrument_ids = []

        for i, target in enumerate(target_deltas):
            abs_target = abs(target)
            
            # Find closest delta for THIS target
            best = min(candidates, key=lambda x: abs(x['abs_delta'] - abs_target))
            
            results_options[i] = best['option']
            results_stats[i] = best
            keep_instrument_ids.append(best['option'].id)

            self.logger.info(
                f"✅ Multi-delta {search_id[:8]} match for Target {target:.3f}: "
                f"Strike ${best['strike']:.0f} Δ={best['delta']:.4f}"
            )

        # 3. Comprehensive cleanup
        self._unsubscribe_from_options(subscribed_ids, keep_instrument_ids=keep_instrument_ids, exclude_search_id=search_id)

        # 4. Final Callback
        if callback:
            callback(search_id, results_options, results_stats)

    def on_instrument(self, instrument: Instrument):
        """
        Called when instrument data is received.
        Handles SPX instrument and option search (both premium and delta).
        
        Args:
            instrument: Instrument that was added
        """
        # Let parent handle primary instrument
        super().on_instrument(instrument)
        
        # Check if this is the SPX instrument
        if instrument.id == self.spx_instrument_id and not self.spx_subscribed:
            self.logger.info(f"SPX instrument received via on_instrument: {instrument.id}")
            
            self.spx_instrument = instrument
            self.subscribe_quote_ticks(self.spx_instrument_id)
            self.spx_subscribed = True
            
            # Notify strategy that SPX is ready
            try:
                self.on_spx_ready()
            except Exception as e:
                self.on_unexpected_error(e)
        
        # Handle options for premium search AND delta search
        # Both use the same _premium_searches dict infrastructure
        self._handle_option_for_premium_search(instrument)
    
    # =========================================================================
    # POSITION MANAGEMENT OVERRIDE FOR OPTIONS
    # =========================================================================
    
    def _get_open_position_for_instrument(self, instrument_id: InstrumentId) -> Optional[Position]:
        """
        Get open position for a specific instrument using EXACT matching.
        
        CRITICAL: For options, each contract is a unique instrument.
        The base class uses fuzzy symbol matching which can incorrectly
        combine different option strikes into one "net position".
        
        This method uses exact InstrumentId matching which is correct
        for option positions.
        
        Args:
            instrument_id: Exact instrument ID to find position for
            
        Returns:
            Position if found, None otherwise
        """
        positions = self.cache.positions_open(instrument_id=instrument_id)
        
        if not positions:
            return None
        
        # Return first matching position
        return positions[0]
    
    def has_open_option_position(self, instrument_id: InstrumentId) -> bool:
        """
        Check if there's an open position for a specific option instrument.
        
        Uses exact InstrumentId matching (not fuzzy symbol matching).
        
        Args:
            instrument_id: Option instrument ID to check
            
        Returns:
            True if position exists, False otherwise
        """
        return self._get_open_position_for_instrument(instrument_id) is not None
    
    def get_all_spx_option_positions(self) -> List[Position]:
        """
        Get all open positions for SPX/SPXW options.
        
        Useful for strategies that manage multiple option positions
        (e.g., spreads, iron condors).
        
        Returns:
            List of all open SPX option positions
        """
        all_positions = self.cache.positions_open()
        spx_options = []
        
        for pos in all_positions:
            # Check if this is an SPX option
            symbol = str(pos.instrument_id.symbol)
            if symbol.startswith("SPXW") or symbol.startswith("SPX"):
                spx_options.append(pos)
        
        return spx_options
    
    def get_net_spx_option_exposure(self) -> Dict[str, float]:
        """
        Calculate net exposure for all SPX option positions.
        
        Returns:
            Dictionary with:
            - 'total_long_qty': Total quantity of long positions
            - 'total_short_qty': Total quantity of short positions
            - 'net_qty': Net quantity (long - short)
            - 'positions_count': Number of open positions
        """
        positions = self.get_all_spx_option_positions()
        
        total_long = 0.0
        total_short = 0.0
        
        for pos in positions:
            qty = float(pos.quantity)
            if pos.side == PositionSide.LONG:
                total_long += qty
            else:
                total_short += qty
        
        return {
            'total_long_qty': total_long,
            'total_short_qty': total_short,
            'net_qty': total_long - total_short,
            'positions_count': len(positions)
        }
    
    # =========================================================================
    # ABSTRACT METHODS - Must be implemented by strategies
    # =========================================================================
    
    @abstractmethod
    def on_spx_ready(self):
        """
        Called when SPX subscription is ready and data is flowing.
        
        Implement this method to:
        - Initialize strategy-specific state
        - Set up timers or schedules
        - Perform any setup that requires SPX data
        """
        pass
    
    @abstractmethod
    def on_spx_tick(self, tick: QuoteTick):
        """
        Called for each SPX quote tick.
        """
        pass

    def on_minute_closed(self, close_price: float):
        """
        Called for each minute close (candle emulation).
        Override in derived strategies to handle signals.
        """
        pass

    def is_opening_range_complete(self) -> bool:
        """Check if opening range calculation is complete."""
        return self.range_calculated and self.or_high is not None and self.or_low is not None

    def is_market_open(self) -> bool:
        """Check if US market is currently open (9:30 AM - 4:00 PM ET)."""
        now_et = self.clock.utc_now().astimezone(self.tz)
        current_time = now_et.time()
        market_close_time = time(16, 0)
        return self.market_open_time <= current_time < market_close_time
