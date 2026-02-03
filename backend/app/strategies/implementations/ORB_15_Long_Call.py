"""
ORB 15-Minute Long Call Strategy

Opening Range Breakout strategy that:
1. Calculates opening range during configurable period (default: 9:30-9:45 AM ET)
2. Enters Long Call when SPX breaks above opening range high
3. Entry conditions: Market open, before cutoff time (configurable), SPX > OR High
4. Position: SPX Call, 0DTE, strike selected by target option price (configurable)
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


class Orb15MinLongCallStrategy(SPXBaseStrategy):
    """
    Opening Range Breakout 15-Minute Long Call Strategy.
    
    Trades SPX 0DTE Call options based on opening range breakout.
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
        
        # Alert throttling (fire only once per SL/TP breach)
        self._sl_alert_fired: bool = False
        self._tp_alert_fired: bool = False

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
            f"🚀 ORB 15-Min Long Call Strategy STARTING: "
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
            or_status = f"OR High={self.or_high:.2f}" if self.or_high else "OR pending"
            daily_high_str = f"{self.daily_high:.2f}" if self.daily_high is not None else "None"
            self.logger.info(
                f"SPX: {self.current_spx_price:.2f} | "
                f"Today High: {daily_high_str} | "
                f"{or_status}"
            )

        # Check for breakout (tick-by-tick breakout detection)
        # REMOVED: Aligning with SPX_15Min_Range to check only on minute close
        # if self.is_opening_range_complete() and not self.breakout_detected:
        #     self._check_breakout()
        
        # Exit condition monitoring moved to on_minute_closed() for throttling
        pass


    # =========================================================================
    # MARKET HOURS & TIMING
    # =========================================================================

    def on_minute_closed(self, close_price: float):
        """Called at each minute close."""
        # Check for breakout - trust close_price from base class
        if self.is_opening_range_complete() and not self.breakout_detected:
            if close_price > self.or_high:
                self.logger.info(f"Breakout detected on minute close: {close_price:.2f} > {self.or_high:.2f}")
                self._trigger_breakout_entry(close_price)
        
        # Monitor active position for exit conditions (once per minute, not per tick)
        if self.active_option_id:
            self._check_sl_order_health()  # Heartbeat: verify SL order is still active
            self._check_exit_conditions()  # Log alerts + trigger software SL if needed

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
        
        # Software SL fallback runs per-tick for fast reaction (only when active)
        if self._software_sl_enabled and self.active_option_id and tick.instrument_id == self.active_option_id:
            mid = (tick.bid_price.as_double() + tick.ask_price.as_double()) / 2
            self._execute_software_sl(mid)

    def _execute_software_sl(self, current_price: float):
        """
        Execute software SL using active_option_id directly.
        
        Overrides base class to use option ID instead of strategy instrument_id,
        which would fail due to symbol mismatch (SPX vs SPXW option).
        """
        if not self._software_sl_enabled or self._software_sl_price is None:
            return
        
        if current_price > self._software_sl_price:
            return  # Price hasn't hit SL yet
        
        self.logger.warning(
            f"🛑 SOFTWARE SL TRIGGERED @ ${current_price:.2f} <= ${self._software_sl_price:.2f}",
            extra={
                "extra": {
                    "event_type": "software_sl_triggered",
                    "current_price": current_price,
                    "sl_price": self._software_sl_price
                }
            }
        )
        self._software_sl_enabled = False  # Prevent multiple triggers
        
        # Use active_option_id directly instead of base class lookup
        if self.active_option_id:
            self.close_strategy_position(
                reason="SOFTWARE_STOP_LOSS",
                override_instrument_id=self.active_option_id
            )
        else:
            self.logger.error("Software SL triggered but no active_option_id set - cannot close")

    # =========================================================================
    # ENTRY LOGIC
    # =========================================================================

    def _trigger_breakout_entry(self, close_price: float):
        """
        Trigger entry after confirmed breakout on minute close.
        
        Args:
            close_price: The minute close price that triggered the breakout.
        """
        if self.breakout_detected or self.entry_attempted_today:
            return
        
        self.logger.info(
            f"🔥 BREAKOUT CONFIRMED! Minute close {close_price:.2f} > "
            f"OR High {self.or_high:.2f}"
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
            return False, "Strategy disabled"
        
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
        """Prepare for entry by searching for Call option via find_option_by_premium."""
        self.logger.info("Preparing entry - searching for Call option")
        self.entry_attempted_today = True
        self.save_state()  # Persist entry attempt immediately
        
        search_id = self.find_option_by_premium(
            target_premium=self.target_option_price,
            option_kind=OptionKind.CALL,
            max_spread=self.max_spread_dollars,
            selection_delay_seconds=self.selection_delay_seconds,
            callback=self._on_call_option_found
        )
        
        if search_id:
            self._active_search_id = search_id
            self.logger.info(f"Option search initiated (search_id: {search_id[:8]}...)")
        else:
            self.logger.error("Failed to initiate option search")

    def _on_call_option_found(self, search_id: str, option: Optional[Instrument], option_data: Optional[Dict]):
        """Callback when find_option_by_premium completes."""
        if search_id != self._active_search_id:
            return
        
        self._active_search_id = None
        
        if option is None:
            self.logger.warning("No suitable Call option found - entry skipped for today")
            return
        
        self.logger.info(
            f"✅ Call option found: Strike ${float(option.strike_price.as_double()):.0f}, "
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
        self.stop_loss_price = rounded_ask * (1 - self.stop_loss_percent / 100)
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
        
        # Check if this was an ENTRY fill - recalculate SL/TP based on actual fill price
        if order_id in self._pending_entry_orders:
            fill_price = float(event.last_px.as_double())
            
            # Recalculate SL/TP using actual fill price (not order price)
            old_sl = self.stop_loss_price
            old_tp = self.take_profit_price
            self.entry_price = fill_price
            self.stop_loss_price = fill_price * (1 - self.stop_loss_percent / 100)
            self.take_profit_price = fill_price + (self.take_profit_dollars / 100)
            
            self.logger.info(
                f"📊 SL/TP RECALCULATED on fill | Fill: ${fill_price:.2f}\n"
                f"   SL: ${old_sl:.2f} → ${self.stop_loss_price:.2f}\n"
                f"   TP: ${old_tp:.2f} → ${self.take_profit_price:.2f}"
            )
            self.save_state()
        
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
        
        # Check software SL fallback first (if broker SL was cancelled/lost)
        self._execute_software_sl(mid_price)
        
        # Check stop loss (throttled - log only once per breach)
        if mid_price <= self.stop_loss_price:
            if not self._sl_alert_fired:
                self.logger.warning(
                    f"⚠️ PASSIVE ALERT: Price hit 🛑 Stop Loss level: ${mid_price:.2f} <= ${self.stop_loss_price:.2f}\n"
                    f"   Broker should be executing the attached SL order. Monitoring for fill report..."
                )
                self._sl_alert_fired = True
            return
        
        # Check take profit (throttled - log only once per breach)
        if mid_price >= self.take_profit_price:
            if not self._tp_alert_fired:
                self.logger.info(
                    f"✨ PASSIVE ALERT: Price hit ✅ Take Profit level: ${mid_price:.2f} >= ${self.take_profit_price:.2f}\n"
                    f"   Broker should be executing the attached TP order. Monitoring for fill report..."
                )
                self._tp_alert_fired = True
            return

    def _clear_active_position_state(self):
        """Reset state after position is closed."""
        self.active_option_id = None
        self.entry_price = None
        self.stop_loss_price = None
        self.take_profit_price = None
        
        # Reset alert throttling flags
        self._sl_alert_fired = False
        self._tp_alert_fired = False
        
        # Reset SL monitoring state
        self._sl_order_id = None
        self._sl_order_active = False
        self._software_sl_enabled = False
        self._software_sl_price = None
        
        self.save_state()

    # =========================================================================
    # LIFECYCLE CLEANUP
    # =========================================================================

    def on_stop_safe(self):
        """Called when strategy stops."""
        self.logger.info("Stopping ORB 15-Min Long Call strategy")
        
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