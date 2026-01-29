"""
ORB 15-Minute Long Put Strategy

Opening Range Breakout strategy that:
1. Calculates opening range during configurable period (default: 9:30-9:45 AM ET)
2. Enters Long Put when SPX breaks below opening range low
3. Entry conditions: Market open, before cutoff time (configurable), SPX < OR Low
4. Position: SPX Put, 0DTE, strike selected by target option price (configurable)
5. Risk management: Configurable stop loss %, configurable take profit $
6. Entry validation: Bid/ask spread < configurable max spread
"""

from typing import Dict, Any, Optional
from datetime import datetime, time, timedelta
import pytz
from decimal import Decimal

from nautilus_trader.model.data import QuoteTick, Bar, BarType, BarSpecification
from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce, OptionKind
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.objects import Quantity, Price
from nautilus_trader.model.instruments import Instrument

from app.strategies.base_spx import SPXBaseStrategy
from app.strategies.config import StrategyConfig


class Orb15MinLongPutStrategy(SPXBaseStrategy):
    """
    Opening Range Breakout 15-Minute Long Put Strategy.
    
    Trades SPX 0DTE Put options based on opening range breakout.
    Uses unified tick-based OR calculation from SPXBaseStrategy.
    """

    def __init__(
        self, 
        config: StrategyConfig, 
        integration_manager=None, 
        persistence_manager=None
    ):
        super().__init__(config, integration_manager, persistence_manager)
        
        # State tracking for logging
        self._last_price_logged_time = 0
        
        # Position management
        self.active_option_id = None
        self.entry_price = None
        self.stop_loss_price = None
        self.take_profit_price = None
        
        # Entry tracking
        self.breakout_detected = False
        self.entry_attempted_today = False
        
        # Search tracking
        self._active_search_id: Optional[str] = None

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    def on_start_safe(self):
        """Called after instrument is ready and base setup complete."""
        super().on_start_safe()
        
        params = self.strategy_config.parameters
        self.target_option_price = float(params.get("target_option_price", 4.0))
        self.stop_loss_percent = float(params.get("stop_loss_percent", 40.0))
        self.take_profit_dollars = float(params.get("take_profit_dollars", 50.0))
        self.max_spread_dollars = float(params.get("max_spread_dollars", 0.2))
        self.cutoff_time_hour = int(params.get("cutoff_time_hour", 15))
        self.quantity = int(self.strategy_config.order_size)
        self.selection_delay_seconds = float(params.get("selection_delay_seconds", 5.0))
        
        self.cutoff_time = time(self.cutoff_time_hour, 0)
        
        self.logger.info(
            f"🚀 ORB 15-Min Long Put Strategy STARTING: "
            f"OR={self.opening_range_minutes}m, Target Price=${self.target_option_price}, "
            f"SL={self.stop_loss_percent}%, TP=${self.take_profit_dollars}, "
            f"Quantity={self.quantity}, Selection Delay={self.selection_delay_seconds}s"
        )

    def on_spx_tick(self, tick: QuoteTick):
        """Called for each SPX quote tick."""
        # Periodically log price (every 60s)
        now_sec = int(self.clock.timestamp_ns() / 1_000_000_000)
        if now_sec % 60 == 0 and now_sec != self._last_price_logged_time:
            self._last_price_logged_time = now_sec
            or_status = f"OR Low={self.or_low:.2f}" if self.or_low else "OR pending"
            daily_low_str = f"{self.daily_low:.2f}" if self.daily_low is not None else "None"
            self.logger.info(
                f"SPX: {self.current_spx_price:.2f} | "
                f"Today Low: {daily_low_str} | "
                f"{or_status}"
            )

        # Check for breakout (tick-by-tick breakout detection)
        # REMOVED: Aligning with SPX_15Min_Range to check only on minute close
        # if self.is_opening_range_complete() and not self.breakout_detected:
        #     self._check_breakout()
        
        # Monitor active position for exit conditions
        if self.active_option_id:
            self._check_exit_conditions()


    # =========================================================================
    # MARKET HOURS & TIMING
    # =========================================================================

    def on_minute_closed(self, close_price: float):
        """Called at each minute close."""
        if self.is_opening_range_complete() and not self.breakout_detected:
            if close_price < self.or_low:
                self.logger.info(f"Breakout detected on minute close: {close_price:.2f} < {self.or_low:.2f}")
                self._check_breakout()

    def _reset_daily_state(self, current_date):
        """Reset daily tracking state. Extends base class reset."""
        super()._reset_daily_state(current_date)
        self.breakout_detected = False
        self.entry_attempted_today = False
        self._active_search_id = None
        self._last_price_logged_time = 0
        self.logger.info(f"Daily strategy state reset for {current_date}")

    # =========================================================================
    # QUOTE HANDLING & PRICE TRACKING
    # =========================================================================

    def on_quote_tick_safe(self, tick: QuoteTick):
        """Handle quote ticks. Routes SPX to base, processes options here."""
        super().on_quote_tick_safe(tick)
        
        # Process option quotes only if we have an active position
        if self.active_option_id and tick.instrument_id == self.active_option_id:
            self._check_exit_conditions()

    # =========================================================================
    # ENTRY LOGIC
    # =========================================================================

    def _check_breakout(self):
        """Check if SPX has broken below opening range low."""
        if self.breakout_detected or self.entry_attempted_today:
            return
        
        if self.daily_low < self.or_low:
            self.logger.info(
                f"🔥 BREAKOUT DETECTED! SPX Low {self.daily_low:.2f} < "
                f"OR Low {self.or_low:.2f}"
            )
            self.breakout_detected = True
            
            # Check entry conditions
            can_enter, reason = self._can_enter()
            if can_enter:
                self._prepare_entry()
            else:
                self.logger.warning(f"Cannot enter: {reason}")

    def _can_enter(self) -> tuple[bool, str]:
        """Check all entry conditions."""
        
        # Check strategy enabled
        if not self.strategy_config.enabled:
            return False, "❌ Strategy disabled"
        
        # Check market hours
        if not self.is_market_open():
            return False, "Market closed"
        
        # Check time cutoff (compare with Eastern Time)
        now_et = self.clock.utc_now().astimezone(self.tz)
        if now_et.time() >= self.cutoff_time:
            return False, f"Past cutoff time ({self.cutoff_time})"
        
        # Check OR calculated
        if not self.is_opening_range_complete():
            return False, "Opening range not yet calculated"
        
        # Check breakout occurred
        if not self.breakout_detected:
            return False, "No breakout detected"
        
        # Check no existing position
        if self._has_open_position():
            return False, "Position already open"
        
        # Check no pending orders
        if self._pending_entry_orders or self._pending_exit_orders:
            return False, "Orders already pending"
        
        # Check SPX price available
        if self.current_spx_price == 0:
            return False, "No SPX price available"
        
        return True, "Ready to enter"

    def _prepare_entry(self):
        """Prepare for entry by searching for Put option via find_option_by_premium."""
        self.logger.info("Preparing entry - searching for Put option")
        self.entry_attempted_today = True
        self.save_state()  # Persist entry attempt immediately
        
        search_id = self.find_option_by_premium(
            target_premium=self.target_option_price,
            option_kind=OptionKind.PUT,
            max_spread=self.max_spread_dollars,
            selection_delay_seconds=self.selection_delay_seconds,
            callback=self._on_put_option_found
        )
        
        if search_id:
            self._active_search_id = search_id
            self.logger.info(f"Option search initiated (search_id: {search_id[:8]}...)")
        else:
            self.logger.error("Failed to initiate option search")

    def _on_put_option_found(self, search_id: str, option: Optional[Instrument], option_data: Optional[Dict]):
        """Callback when find_option_by_premium completes."""
        if search_id != self._active_search_id:
            return
        
        self._active_search_id = None
        
        if option is None:
            self.logger.warning("No suitable Put option found - entry skipped for today")
            return
        
        self.logger.info(
            f"✅ Put option found: Strike ${float(option.strike_price.as_double()):.0f}, "
            f"Mid=${option_data['mid']:.2f}, Spread=${option_data['spread']:.2f}"
        )
        
        # Try to enter with selected option
        self._try_entry_with_option(option, option_data)

    def _try_entry_with_option(self, option: Instrument, option_data: Dict):
        """Attempt to enter position with the selected option contract."""
        ask = option_data['ask']
        
        # Round ask price to conform to tick size
        rounded_ask = self.round_to_tick(ask, option)
        
        # Create entry limit order
        order = self.order_factory.limit(
            instrument_id=option.id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_int(self.quantity),
            price=Price.from_str(str(rounded_ask)),
            time_in_force=TimeInForce.DAY
        )
        
        # Calculate SL/TP prices based on rounded entry price
        # Stop Loss: rounded entry * (1 - SL%)
        self.stop_loss_price = rounded_ask * (1 - self.stop_loss_percent / 100)
        # Take Profit: rounded entry + TP dollars per share (multiplier 100)
        self.take_profit_price = rounded_ask + (self.take_profit_dollars / 100)
        
        # Submit bracket order (entry + target SL/TP)
        success = self.submit_bracket_order(
            entry_order=order,
            stop_loss_price=self.stop_loss_price,
            take_profit_price=self.take_profit_price
        )
        
        if success:
            self.active_option_id = option.id
            self.entry_price = rounded_ask
            
            self.logger.info(
                f"📈 BRACKET ORDER SUBMITTED: {option.id} @ ${rounded_ask:.2f} (original: ${ask:.2f})\n"
                f"   SL: ${self.stop_loss_price:.2f} ({self.stop_loss_percent}%)\n"
                f"   TP: ${self.take_profit_price:.2f} (+${self.take_profit_dollars})"
            )
            
            self.save_state()

    def on_order_filled_safe(self, event: OrderFilled):
        """Called when an order is filled."""
        order_id = event.client_order_id
        
        # Check if this was our active option exit
        if self.active_option_id and order_id in self._pending_exit_orders:
            # Position closed (either by SL, TP, or manual)
            self.logger.info(f"Active position closed via fill: {order_id}")
            self._clear_active_position_state()

    # =========================================================================
    # EXIT LOGIC
    # =========================================================================

    def _check_exit_conditions(self):
        """Monitor position and check for exit conditions (SL/TP)."""
        if not self.active_option_id:
            return
        
        # Get current option quote
        quote = self.cache.quote_tick(self.active_option_id)
        
        if not quote:
            return
        
        bid = quote.bid_price.as_double()
        ask = quote.ask_price.as_double()
        
        if bid <= 0 or ask <= 0:
            return
        
        mid_price = (bid + ask) / 2
        
        # Check stop loss
        if mid_price <= self.stop_loss_price:
            self.logger.warning(
                f"⚠️ PASSIVE ALERT: Price hit 🛑 Stop Loss level: ${mid_price:.2f} <= ${self.stop_loss_price:.2f}\n"
                f"   Broker should be executing the attached SL order. Monitoring for fill report..."
            )
            return
        
        # Check take profit
        if mid_price >= self.take_profit_price:
            self.logger.info(
                f"✨ PASSIVE ALERT: Price hit ✅ Take Profit level: ${mid_price:.2f} >= ${self.take_profit_price:.2f}\n"
                f"   Broker should be executing the attached TP order. Monitoring for fill report..."
            )
            return

    def _clear_active_position_state(self):
        """Reset state after position is closed."""
        self.active_option_id = None
        self.entry_price = None
        self.stop_loss_price = None
        self.take_profit_price = None
        
        self.save_state()

    # =========================================================================
    # LIFECYCLE CLEANUP
    # =========================================================================

    def on_stop_safe(self):
        """Called when strategy stops."""
        self.logger.info("Stopping ORB 15-Min Long Put strategy")
        
        # Cancel any active search
        if self._active_search_id:
            self.cancel_premium_search(self._active_search_id)
            self._active_search_id = None
        
        # Close any open positions
        if self._has_open_position():
            self.close_strategy_position(reason="STRATEGY_STOP")
        
        # Unsubscribe from option quotes
        if self.active_option_id:
            try:
                self.unsubscribe_quote_ticks(self.active_option_id)
            except Exception as e:
                self.logger.error(f"Failed to unsubscribe from option: {e}")

    # =========================================================================
    # STATE PERSISTENCE
    # =========================================================================

    def get_state(self) -> Dict[str, Any]:
        """Return strategy state for persistence."""
        state = super().get_state()
        state.update({
            "breakout_detected": self.breakout_detected,
            "entry_attempted_today": self.entry_attempted_today,
            "active_option_id": str(self.active_option_id) if self.active_option_id else None,
            "entry_price": self.entry_price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
        })
        return state

    def set_state(self, state: Dict[str, Any]):
        """Restore strategy state."""
        super().set_state(state)
        self.breakout_detected = state.get("breakout_detected", False)
        self.entry_attempted_today = state.get("entry_attempted_today", False)
        
        option_id_str = state.get("active_option_id")
        if option_id_str:
            self.active_option_id = InstrumentId.from_str(option_id_str)
        
        self.entry_price = state.get("entry_price")
        self.stop_loss_price = state.get("stop_loss_price")
        self.take_profit_price = state.get("take_profit_price")