"""
SPXBaseStrategy - Base class for all SPX-related strategies

Provides:
- Unified SPX instrument subscription with fallback mechanism
- SPX price tracking and tick handling
- State persistence for SPX subscription status
- Abstract methods for strategy-specific SPX logic
- Proper resource cleanup on stop
"""

from abc import abstractmethod
from typing import Dict, Any
import asyncio

from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.instruments import Instrument

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
            
            # Start fallback polling mechanism
            asyncio.create_task(self._wait_for_spx_and_subscribe())
    
    async def _wait_for_spx_and_subscribe(self):
        """
        Fallback polling to ensure SPX subscription if on_instrument doesn't fire immediately.
        
        Polls cache for up to 30 seconds waiting for SPX instrument to be available.
        """
        self.logger.info("Starting SPX availability polling (30s timeout)")
        
        for attempt in range(30):
            # Check if SPX is now in cache
            self.spx_instrument = self.cache.instrument(self.spx_instrument_id)
            
            if self.spx_instrument:
                self.logger.info(f"SPX instrument found via polling (attempt {attempt + 1}), subscribing...")
                
                # Subscribe to quote ticks
                self.subscribe_quote_ticks(self.spx_instrument_id)
                self.spx_subscribed = True
                
                # Notify strategy that SPX is ready
                try:
                    self.on_spx_ready()
                except Exception as e:
                    self.on_unexpected_error(e)
                
                return
            
            # Wait 1 second before next attempt
            await asyncio.sleep(1)
        
        # Timeout - SPX not available
        self.logger.error(
            f"Timeout waiting for SPX instrument {self.spx_instrument_id} from IB. "
            "Strategy may not function correctly."
        )
    
    def on_instrument(self, instrument: Instrument):
        """
        Called when instrument data is received.
        Handles SPX instrument addition.
        
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
