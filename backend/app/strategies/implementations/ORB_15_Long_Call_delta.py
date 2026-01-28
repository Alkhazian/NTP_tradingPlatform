"""
ORB 15-Minute Long Call Delta Strategy

Opening Range Breakout strategy that:
1. Calculates opening range during configurable period (default: 9:30-9:45 AM ET)
2. Enters Long Call when SPX breaks above opening range high
3. Entry conditions: Market open, before cutoff time (configurable), SPX > OR High
4. Position: SPX Call, 0DTE, strike selected by target delta (configurable, default 0.25)
5. Risk management: Configurable stop loss %, configurable take profit $
6. Entry validation: Bid/ask spread < configurable max spread
"""

from typing import Dict, Any, Optional
from datetime import datetime, time, timedelta
import pytz
from decimal import Decimal

from nautilus_trader.model.data import QuoteTick, Bar, BarType, BarSpecification
from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce, OptionKind
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.objects import Quantity, Price
from nautilus_trader.model.instruments import Instrument

from app.strategies.base_spx import SPXBaseStrategy
from app.strategies.config import StrategyConfig


class Orb15MinLongCallDeltaStrategy(SPXBaseStrategy):
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
        
        # Tracking for option selection
        self.options_requested = False
        self.requested_strikes = []
        self.received_options = []  # Track all received options for selection


    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    def on_start_safe(self):
        """Called after instrument is ready and base setup complete."""
        super().on_start_safe()
        
        # Load parameters (already handled by base mostly, but local ones here)
        params = self.strategy_config.parameters
        self.target_delta = float(params.get("target_delta", 0.25))
        self.stop_loss_percent = float(params.get("stop_loss_percent", 40.0))
        self.take_profit_dollars = float(params.get("take_profit_dollars", 50.0))
        self.max_spread_dollars = float(params.get("max_spread_dollars", 0.2))
        self.cutoff_time_hour = int(params.get("cutoff_time_hour", 15))
        self.quantity = int(self.strategy_config.order_size)
        self.selection_delay_seconds = float(params.get("selection_delay_seconds", 10.0))
        
        # Eastern Time cutoff for entries
        self.cutoff_time = time(self.cutoff_time_hour, 0)
        
        self.logger.info(
            f"🚀 ORB 15-Min Long Call Delta Strategy STARTING: "
            f"OR={self.opening_range_minutes}m, Target Delta={self.target_delta}, "
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
        if self.is_opening_range_complete() and not self.breakout_detected:
            self._check_breakout()
        
        # Monitor active position for exit conditions
        if self.active_option_id:
            self._check_exit_conditions()


    # =========================================================================
    # MARKET HOURS & TIMING
    # =========================================================================

    def on_minute_closed(self, close_price: float):
        """Called at each minute close. Can be used for smoother signals."""
        if self.is_opening_range_complete() and not self.breakout_detected:
            # We also check breakout on minute close for consistency with bar-based methods
            if close_price > self.or_high:
                self.logger.info(f"Breakout detected on minute close: {close_price:.2f} > {self.or_high:.2f}")
                self._check_breakout()

    def _reset_daily_state(self, current_date):
        """Reset daily tracking state. Extends base class reset."""
        super()._reset_daily_state(current_date)
        self.breakout_detected = False
        self.entry_attempted_today = False
        self.options_requested = False
        self.requested_strikes = []
        self.received_options = []
        self._last_price_logged_time = 0
        self.logger.info(f"Daily strategy state reset for {current_date}")

    # =========================================================================
    # QUOTE HANDLING & PRICE TRACKING
    # =========================================================================

    # Quote handling is now integrated into base class SPXBaseStrategy.
    # on_spx_tick() and on_minute_closed() are used instead.
    
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
        """Check if SPX has broken above opening range high."""
        if self.breakout_detected or self.entry_attempted_today:
            return
        
        if self.daily_high > self.or_high:
            self.logger.info(
                f"🔥 BREAKOUT DETECTED! SPX High {self.daily_high:.2f} > "
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
        """Prepare for entry by requesting appropriate option contracts."""
        self.logger.info("Preparing entry - requesting option contracts")
        
        # Calculate base strike (ATM)
        base_strike = self._calculate_target_strike()
        
        if base_strike is None:
            self.logger.error("Failed to calculate base strike")
            return
        
        # Request range of strikes to find best price match
        self._request_call_options(base_strike)
        
        self.entry_attempted_today = True
        self.save_state()  # Save state immediately to prevent repeated attempts on restart

    def _calculate_target_strike(self) -> Optional[float]:
        """
        Calculate target strike price based on Delta target.
        We want an option with Delta close to target_delta (e.g., 0.25).
        
        Strategy: Request a range of strikes and select the one closest to target price.
        """
        if self.current_spx_price == 0:
            return None
        
        # We'll request multiple strikes around ATM and pick the best one
        # This is a placeholder - we'll request a range in _request_call_options
        atm_strike = round(self.current_spx_price / 5) * 5
        
        self.logger.info(
            f"Will request strike range around ATM ${atm_strike:.0f} "
            f"to find option with Delta near {self.target_delta}"
        )
        
        return atm_strike

    def _request_call_options(self, base_strike: float):
        """
        Request multiple SPX Call option contracts to find best price match.
        Requests strikes: ATM, ATM+5, ATM+10, ATM+15, ATM+20
        """
        
        # Get today's date (0DTE)
        today = self.clock.utc_now().date()
        expiry_date_ib = today.strftime("%Y%m%d")
        
        # Request 7 strikes to find the one priced closest to target
        strikes_to_request = [
            base_strike,
            base_strike + 5,
            base_strike + 10,
            base_strike + 15,
            base_strike + 20,
            base_strike + 25,
            base_strike + 30
        ]
        
        self.logger.info(
            f"Requesting SPX Calls to find option with Delta near {self.target_delta}: "
            f"Strikes={strikes_to_request}, Expiry={expiry_date_ib}"
        )
        
        try:
            contracts = []
            for strike in strikes_to_request:
                contracts.append({
                    "secType": "OPT",
                    "symbol": "SPX",
                    "tradingClass": "SPXW",
                    "exchange": "CBOE",
                    "currency": "USD",
                    "lastTradeDateOrContractMonth": expiry_date_ib,
                    "strike": float(strike),
                    "right": "C",
                    "multiplier": "100"
                })
            
            self.request_instruments(
                venue=Venue("CBOE"),
                params={"ib_contracts": contracts}
            )
            
            self.options_requested = True
            self.requested_strikes = strikes_to_request
            
            self.logger.info(f"✅ Requested {len(contracts)} SPX Call options")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to request Call options: {e}", exc_info=True)

    def on_instrument(self, instrument: Instrument):
        """Called when new instrument is added to cache."""
        super().on_instrument(instrument)
        
        # Diagnostic: Log every instrument received during selection window
        if self.options_requested and not self.active_option_id:
            kind_str = "OTHER"
            if hasattr(instrument, 'option_kind'):
                kind_str = "CALL" if instrument.option_kind == OptionKind.CALL else "PUT"
            
            self.logger.info(
                f"📥 Received instrument: {instrument.id} (Kind={kind_str}, "
                f"Strike={getattr(instrument, 'strike_price', 'N/A')})"
            )
        
        # Collect all received options for selection
        if (self.options_requested and
            not self.active_option_id and
            hasattr(instrument, 'option_kind') and
            instrument.option_kind == OptionKind.CALL):
            
            self.logger.info(f"Received option: {instrument.id}, Strike: {instrument.strike_price}")
            self.received_options.append(instrument)
            
            # Subscribe to option quotes immediately so Greeks can be calculated
            self.subscribe_quote_ticks(instrument.id)
            
            # After receiving options, try to select the best one
            # Use one-shot time alert (cancel existing to avoid duplicates)
            timer_name = f"{self.id}.select_option"
            try:
                self.clock.cancel_timer(timer_name)
            except Exception:
                pass  # Timer may not exist
            
            self.clock.set_time_alert(
                name=timer_name,
                alert_time=self.clock.utc_now() + timedelta(seconds=self.selection_delay_seconds),
                callback=self._select_and_enter_best_option
            )

    def _select_and_enter_best_option(self, timer_event):
        """Select the option with price closest to target and attempt entry."""
        
        if self.active_option_id:
            return  # Already entered
        
        if not self.received_options:
            self.logger.warning("No options received for selection")
            return
        
        self.logger.info(f"Selecting best option from {len(self.received_options)} received contracts")
        
        # Get Delta for all received options
        option_data = []
        
        for option in self.received_options:
            # Quote already subscribed in on_instrument, check if available
            # Give a fallback subscription in case it wasn't subscribed
            try:
                self.subscribe_quote_ticks(option.id)
            except Exception:
                pass  # Already subscribed
            
            # Use GreeksCalculator
            try:
                quote = self.cache.quote_tick(option.id)
                greeks = self.greeks.instrument_greeks(option.id)
                
                mid = 0.0
                spread = 0.0
                if quote:
                    bid = quote.bid_price.as_double()
                    ask = quote.ask_price.as_double()
                    if bid > 0 and ask > 0:
                        mid = (bid + ask) / 2
                        spread = ask - bid
                else:
                    self.logger.warning(f"  Strike ${float(option.strike_price.as_double()):.0f}: No quote yet")

                if greeks and greeks.delta:
                    delta = abs(float(greeks.delta))
                    gamma = float(greeks.gamma) if greeks.gamma else 0.0
                    theta = float(greeks.theta) if greeks.theta else 0.0
                    vega = float(greeks.vega) if greeks.vega else 0.0
                    
                    option_data.append({
                        'option': option,
                        'delta': delta,
                        'mid': mid,
                        'spread': spread
                    })
                    
                    self.logger.info(
                        f"  Strike ${float(option.strike_price.as_double()):.0f}: "
                        f"Delta={delta:.3f} (γ={gamma:.4f}, θ={theta:.2f}, ν={vega:.2f}) | "
                        f"Mid=${mid:.2f}, Spread=${spread:.2f}"
                    )
                else:
                    reason = "Greeks missing" if greeks else "No response for Greeks yet"
                    self.logger.warning(
                        f"  Strike ${float(option.strike_price.as_double()):.0f}: {reason} | "
                        f"Mid=${mid:.2f}"
                    )
            except Exception as e:
                self.logger.warning(f"Could not calculate Greeks for {option.id}: {e}")
        
        if not option_data:
            self.logger.warning("No options with valid Delta available")
            return
        
        # Find option with Delta closest to target (target_delta)
        target_delta = self.target_delta
        best_option_data = min(
            option_data,
            key=lambda x: abs(x['delta'] - target_delta)
        )
        
        selected_option = best_option_data['option']
        selected_delta = best_option_data['delta']
        selected_mid = best_option_data['mid']
        selected_spread = best_option_data['spread']
        
        self.logger.info(
            f"✅ Selected option: Strike ${float(selected_option.strike_price.as_double()):.0f}, "
            f"Delta={selected_delta:.3f} (closest to target {target_delta}), "
            f"Mid=${selected_mid:.2f}, Spread=${selected_spread:.2f}"
        )
        
        # Try to enter with selected option
        self._try_entry_with_option(selected_option)

    def _try_entry_with_option(self, option: Instrument):
        """
        Attempt to enter position with the received option contract.
        
        IMPORTANT: This is called ONLY ONCE per day after option selection.
        If spread is too wide, entry is SKIPPED for the day - NO RETRIES.
        """
        
        # Get option quote
        quote = self.cache.quote_tick(option.id)
        
        if not quote:
            self.logger.warning(f"No quote available for {option.id} - will not retry")
            return
        
        bid = quote.bid_price.as_double()
        ask = quote.ask_price.as_double()
        
        if bid <= 0 or ask <= 0:
            self.logger.warning(
                f"Invalid quote for {option.id}: bid={bid}, ask={ask} - will not retry"
            )
            return
        
        # Check spread - THIS IS THE ONLY ATTEMPT
        spread = ask - bid
        if spread > self.max_spread_dollars:
            self.logger.warning(
                f"❌ ENTRY SKIPPED - Spread too wide: ${spread:.2f} > ${self.max_spread_dollars}\n"
                f"   This was the only entry attempt for today (no retries)"
            )
            # Entry attempted and failed - won't try again today
            return
        
        mid_price = (bid + ask) / 2
        
        self.logger.info(
            f"Option quote: {option.id} - "
            f"Bid=${bid:.2f}, Ask=${ask:.2f}, Mid=${mid_price:.2f}, "
            f"Spread=${spread:.2f} ✅"
        )
        
        # Round ask price to conform to tick size
        rounded_ask = self.round_to_tick(ask, option)
        
        # Create limit order at ask price
        order = self.order_factory.limit(
            instrument_id=option.id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_int(self.quantity),
            price=Price.from_str(str(rounded_ask)),
            time_in_force=TimeInForce.DAY
        )
        
        # Submit entry order
        if self.submit_entry_order(order):
            self.active_option_id = option.id
            self.entry_price = rounded_ask
            
            # Calculate exit levels based on rounded entry price
            # Stop Loss: 40% below entry price
            self.stop_loss_price = rounded_ask * (1 - self.stop_loss_percent / 100)
            
            # Take Profit: entry + take_profit_dollars PER CONTRACT
            # Note: Option price is per share, multiply by 100 for contract value
            self.take_profit_price = rounded_ask + (self.take_profit_dollars / 100)
            
            self.logger.info(
                f"📈 ENTRY ORDER SUBMITTED: {option.id} @ ${rounded_ask:.2f} (Limit) (original: ${ask:.2f})\n"
                f"   Entry per share: ${rounded_ask:.2f}\n"
                f"   Entry per contract: ${rounded_ask * 100:.2f}\n"
                f"   Stop Loss per share: ${self.stop_loss_price:.2f} "
                f"({self.stop_loss_percent}%)\n"
                f"   Stop Loss per contract: ${self.stop_loss_price * 100:.2f}\n"
                f"   Take Profit per share: ${self.take_profit_price:.2f}\n"
                f"   Take Profit per contract: ${self.take_profit_price * 100:.2f} "
                f"(+${self.take_profit_dollars})"
            )
            
            # Subscribe to option quotes for monitoring
            self.subscribe_quote_ticks(option.id)
            
            self.save_state()


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
            self.logger.info(
                f"🛑 STOP LOSS HIT: ${mid_price:.2f} <= ${self.stop_loss_price:.2f}"
            )
            self._exit_position("STOP_LOSS")
            return
        
        # Check take profit
        if mid_price >= self.take_profit_price:
            self.logger.info(
                f"✅ TAKE PROFIT HIT: ${mid_price:.2f} >= ${self.take_profit_price:.2f}"
            )
            self._exit_position("TAKE_PROFIT")
            return

    def _exit_position(self, reason: str):
        """Exit the position."""
        if not self.active_option_id:
            return
        
        self.logger.info(f"Exiting position: {reason}")
        
        # Use base method to close position
        self.close_strategy_position(reason=reason)
        
        # Reset state
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
        self.logger.info("Stopping ORB 15-Min Long Call strategy")
        
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
            "options_requested": self.options_requested,
            "requested_strikes": self.requested_strikes,
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
        self.options_requested = state.get("options_requested", False)
        self.requested_strikes = state.get("requested_strikes", [])