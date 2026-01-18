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
from datetime import timedelta
from decimal import Decimal


from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.enums import OptionKind

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
        
        self.logger.info(f"SPXBaseStrategy initialized with SPX instrument: {self.spx_instrument_id}")
    
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
    
    def on_quote_tick(self, tick: QuoteTick):
        """
        Handle quote tick events.
        Routes SPX ticks to on_spx_tick(), other ticks to parent.
        
        Args:
            tick: Quote tick data
        """
        try:
            # Check if this is an SPX tick
            if tick.instrument_id == self.spx_instrument_id:
                # Update current SPX price
                bid = tick.bid_price.as_double()
                ask = tick.ask_price.as_double()
                
                if bid > 0 and ask > 0:
                    self.current_spx_price = (bid + ask) / 2
                elif bid > 0:
                    self.current_spx_price = bid
                elif ask > 0:
                    self.current_spx_price = ask
                
                # Call strategy-specific SPX tick handler
                self.on_spx_tick(tick)
            else:
                # Not an SPX tick - let parent handle it
                # This allows strategies to subscribe to other instruments
                pass
        except Exception as e:
            self.on_unexpected_error(e)
    
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
        
        # Call parent cleanup
        super().on_stop_safe()
    
    # =========================================================================
    # STATE PERSISTENCE
    # =========================================================================
    
    def get_state(self) -> Dict[str, Any]:
        """
        Return strategy-specific state to persist.
        
        Returns:
            Dictionary containing SPX state
        """
        state = super().get_state()
        state.update({
            "current_spx_price": self.current_spx_price,
            "spx_subscribed": self.spx_subscribed,
        })
        return state
    
    def set_state(self, state: Dict[str, Any]):
        """
        Restore strategy-specific state.
        
        Args:
            state: Dictionary containing saved state
        """
        super().set_state(state)
        self.current_spx_price = state.get("current_spx_price", 0.0)
        self.spx_subscribed = state.get("spx_subscribed", False)
        
        self.logger.info(
            f"SPX state restored: price={self.current_spx_price:.2f}, "
            f"subscribed={self.spx_subscribed}"
        )
    
    # =========================================================================
    # OPTION SEARCH BY PREMIUM
    # =========================================================================
    
    def find_option_by_premium(
        self,
        target_premium: float,
        option_kind: OptionKind,
        expiry_date: Optional[str] = None,
        strike_range: int = 7,
        strike_step: int = 5,
        max_spread: Optional[float] = None,
        selection_delay_seconds: float = 2.0,
        callback: Optional[Callable[[Optional[Instrument], Optional[Dict]], None]] = None
    ):
        """
        Search for SPX options by target premium (option price).
        
        This method requests multiple strikes around ATM, collects quotes,
        and finds the option with price closest to target premium.
        
        Args:
            target_premium: Target option price (e.g., 4.0 for $4 option)
            option_kind: OptionKind.CALL or OptionKind.PUT
            expiry_date: Expiry date in YYYYMMDD format, default today (0DTE)
            strike_range: Number of strikes to request (default 7)
            strike_step: Step between strikes in points (default 5)
            max_spread: Maximum allowed bid-ask spread, None = no filter
            selection_delay_seconds: Delay before selecting best option (default 2.0)
            callback: Function to call with (selected_option, option_data) or (None, None) if failed
        
        Returns:
            None - results delivered via callback
            
        Example:
            self.find_option_by_premium(
                target_premium=4.0,
                option_kind=OptionKind.CALL,
                max_spread=0.20,
                callback=self._on_option_found
            )
        """
        if self.current_spx_price == 0:
            self.logger.error("Cannot search for options: SPX price not available")
            if callback:
                callback(None, None)
            return
        
        # Initialize search state
        self._premium_search_state = {
            'target_premium': target_premium,
            'option_kind': option_kind,
            'max_spread': max_spread,
            'callback': callback,
            'received_options': [],
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
                venue=Venue("InteractiveBrokers"),
                params={"ib_contracts": contracts}
            )
            
            self.logger.info(f"✅ Requested {len(contracts)} SPX {option_kind.name} options")
            
            # Set timer to select best option after delay
            timer_name = f"{self.id}.select_premium_option"
            try:
                self.clock.cancel_timer(timer_name)
            except Exception:
                pass
            
            self.clock.set_time_alert(
                name=timer_name,
                alert_time=self.clock.utc_now() + timedelta(seconds=selection_delay_seconds),
                callback=self._on_premium_search_complete
            )
            
        except Exception as e:
            self.logger.error(f"❌ Failed to request options: {e}", exc_info=True)
            if callback:
                callback(None, None)
    
    def _handle_option_for_premium_search(self, instrument: Instrument):
        """
        Internal handler for options received during premium search.
        Called from on_instrument().
        
        Args:
            instrument: Received option instrument
        """
        if not hasattr(self, '_premium_search_state') or not self._premium_search_state.get('active'):
            return
        
        # Check if this is the option type we're looking for
        if not hasattr(instrument, 'option_kind'):
            return
        
        if instrument.option_kind != self._premium_search_state['option_kind']:
            return
        
        self.logger.debug(f"Received option for premium search: {instrument.id}")
        
        # Subscribe to quotes for this option
        self.subscribe_quote_ticks(instrument.id)
        
        # Add to received options
        self._premium_search_state['received_options'].append(instrument)
    
    def _on_premium_search_complete(self, timer_event):
        """
        Timer callback to select best option after receiving options.
        Finds option with price closest to target premium.
        """
        if not hasattr(self, '_premium_search_state') or not self._premium_search_state.get('active'):
            return
        
        state = self._premium_search_state
        state['active'] = False  # Mark search complete
        
        received_options = state['received_options']
        target_premium = state['target_premium']
        max_spread = state['max_spread']
        callback = state['callback']
        
        if not received_options:
            self.logger.warning("No options received for premium search")
            if callback:
                callback(None, None)
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
        
        if not option_prices:
            self.logger.warning("No valid option quotes available after filtering")
            if callback:
                callback(None, None)
            return
        
        # Find option closest to target premium
        best_option_data = min(
            option_prices,
            key=lambda x: abs(x['mid'] - target_premium)
        )
        
        selected_option = best_option_data['option']
        
        self.logger.info(
            f"✅ Selected: Strike ${best_option_data['strike']:.0f}, "
            f"Mid=${best_option_data['mid']:.2f} "
            f"(target: ${target_premium:.2f}), "
            f"Spread=${best_option_data['spread']:.2f}"
        )
        
        # Call callback with result
        if callback:
            callback(selected_option, best_option_data)
    
    def on_instrument(self, instrument: Instrument):
        """
        Called when instrument data is received.
        Handles SPX instrument and option search.
        
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
        
        # Handle options for premium search
        self._handle_option_for_premium_search(instrument)
    
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
        
        Args:
            tick: SPX quote tick data
            
        Implement this method to:
        - React to SPX price changes
        - Update strategy logic based on SPX movement
        - Trigger entry/exit conditions
        
        Note: self.current_spx_price is automatically updated before this is called
        """
        pass
