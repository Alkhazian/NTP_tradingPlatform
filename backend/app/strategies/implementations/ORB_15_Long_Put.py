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
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.objects import Quantity, Price
from nautilus_trader.model.instruments import Instrument

from app.strategies.base import BaseStrategy
from app.strategies.config import StrategyConfig


class Orb15MinLongPutStrategy(BaseStrategy):
    """
    Opening Range Breakout 15-Minute Long Put Strategy.
    
    Trades SPX 0DTE Put options based on opening range breakout.
    """

    def __init__(
        self, 
        config: StrategyConfig, 
        integration_manager=None, 
        persistence_manager=None
    ):
        super().__init__(config, integration_manager, persistence_manager)
        
        # Strategy parameters
        self.opening_range_minutes = int(
            self.strategy_config.parameters.get("opening_range_minutes", 15)
        )
        self.target_option_price = float(
            self.strategy_config.parameters.get("target_option_price", 4.0)
        )
        self.stop_loss_percent = float(
            self.strategy_config.parameters.get("stop_loss_percent", 40.0)
        )
        self.take_profit_dollars = float(
            self.strategy_config.parameters.get("take_profit_dollars", 50.0)
        )
        self.max_spread_dollars = float(
            self.strategy_config.parameters.get("max_spread_dollars", 0.2)
        )
        self.cutoff_time_hour = int(
            self.strategy_config.parameters.get("cutoff_time_hour", 15)  # 3 PM
        )
        self.quantity = int(
            self.strategy_config.parameters.get("quantity", 1)
        )
        
        # Market hours (Eastern Time)
        self.eastern_tz = pytz.timezone('US/Eastern')
        self.market_open_time = time(9, 30)   # 9:30 AM ET
        self.market_close_time = time(16, 0)  # 4:00 PM ET
        self.or_end_time = None  # Calculated dynamically each day
        self.cutoff_time = time(self.cutoff_time_hour, 0)  # Default 3:00 PM ET
        
        # Opening Range state
        self.or_high = None
        self.or_low = None
        self.or_calculated = False
        self.or_bars = []
        self.last_or_calculation_date = None
        self.last_reset_date = None

        
        # SPX price tracking
        self.current_spx_price = 0.0
        self.current_spx_low = 1_000_000.0  # Today's low, init to high number so first price sets it
        self.last_quote_time_ns = 0
        
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
        """Called after instrument is ready and base setup complete.
        
        Note: Data subscriptions are handled by _subscribe_data() called from base class.
        """
        self.logger.info(
            f"ORB 15-Min Long Put Strategy starting: "
            f"OR={self.opening_range_minutes}m, "
            f"Target option price=${self.target_option_price}, "
            f"SL={self.stop_loss_percent}%, "
            f"TP=${self.take_profit_dollars} per contract, "
            f"Max spread=${self.max_spread_dollars}"
        )

    def _subscribe_data(self):
        """Subscribe to required data feeds."""
        # Called by BaseStrategy - subscribe to quotes and bars
        
        # Subscribe to SPX quotes
        try:
            self.subscribe_quote_ticks(self.instrument_id)
            self.logger.info(f"Subscribed to SPX quotes: {self.instrument_id}")
        except Exception as e:
            self.logger.error(f"Failed to subscribe to SPX quotes: {e}", exc_info=True)
        
        # Subscribe to 1-minute bars for opening range calculation
        try:
            bar_type = BarType(
                self.instrument_id,
                BarSpecification.from_str("1-MINUTE-LAST")
            )
            self.subscribe_bars(bar_type)
            self.logger.info(f"Subscribed to 1-minute bars for OR calculation")
        except Exception as e:
            self.logger.error(f"Failed to subscribe to bars: {e}", exc_info=True)


    # =========================================================================
    # MARKET HOURS & TIMING
    # =========================================================================

    def _get_eastern_now(self) -> datetime:
        """Get current time in Eastern timezone using strategy clock (backtestable)."""
        return self.clock.utc_now().astimezone(self.eastern_tz)

    def is_market_open(self) -> bool:
        """Check if SPX options market is currently open."""
        now = self._get_eastern_now()
        
        # Weekend check
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        
        # Market hours check
        current_time = now.time()
        return self.market_open_time <= current_time <= self.market_close_time

    def is_before_cutoff(self) -> bool:
        """Check if current time is before entry cutoff (default 3 PM ET)."""
        now = self._get_eastern_now()
        return now.time() < self.cutoff_time

    def get_or_end_time(self) -> time:
        """Calculate opening range end time (9:30 + opening_range_minutes)."""
        if self.or_end_time is None:
            # Calculate OR end time
            market_open = datetime.combine(datetime.today(), self.market_open_time)
            or_end = market_open + timedelta(minutes=self.opening_range_minutes)
            self.or_end_time = or_end.time()
        return self.or_end_time

    def is_in_opening_range_period(self) -> bool:
        """Check if we're currently in the opening range period."""
        now = self._get_eastern_now()
        current_time = now.time()
        or_end = self.get_or_end_time()
        
        return self.market_open_time <= current_time < or_end

    def should_reset_daily_state(self) -> bool:
        """Check if we need to reset daily state (new trading day)."""
        now = self._get_eastern_now()
        today = now.date()
        
        if self.last_reset_date != today:
            return True
        return False


    # =========================================================================
    # OPENING RANGE CALCULATION
    # =========================================================================

    def on_bar_safe(self, bar: Bar):
        """Handle 1-minute bars for opening range calculation."""
        
        # Reset daily state if new day
        if self.should_reset_daily_state():
            self._reset_daily_state()
        
        # Update current low
        bar_low = bar.low.as_double()
        if bar_low < self.current_spx_low:
            self.current_spx_low = bar_low
        
        # Collect bars during opening range period
        if self.is_in_opening_range_period() and not self.or_calculated:
            self.or_bars.append(bar)
            self.logger.debug(
                f"OR bar collected: H={bar.high.as_double():.2f}, "
                f"L={bar.low.as_double():.2f} "
                f"({len(self.or_bars)}/{self.opening_range_minutes})"
            )
        
        # Calculate opening range when period ends
        elif not self.or_calculated and len(self.or_bars) > 0:
            self._calculate_opening_range()
        
        # Check for breakout after OR calculated
        if self.or_calculated and not self.breakout_detected:
            self._check_breakout()

    def _calculate_opening_range(self):
        """Calculate opening range high and low from collected bars."""
        if not self.or_bars:
            self.logger.warning("No bars collected for opening range calculation")
            return
        
        highs = [bar.high.as_double() for bar in self.or_bars]
        lows = [bar.low.as_double() for bar in self.or_bars]
        
        self.or_high = max(highs)
        self.or_low = min(lows)
        self.or_calculated = True
        
        now = self._get_eastern_now()
        self.last_or_calculation_date = now.date()
        
        self.logger.info(
            f"✅ Opening Range calculated ({self.opening_range_minutes}m): "
            f"High={self.or_high:.2f}, Low={self.or_low:.2f}, "
            f"Range=${self.or_high - self.or_low:.2f}"
        )

    def _reset_daily_state(self):
        """Reset all daily state for new trading day."""
        self.logger.info("Resetting daily state for new trading day")
        
        # Update reset date tracking
        now = self._get_eastern_now()
        self.last_reset_date = now.date()
        
        
        self.or_high = None
        self.or_low = None
        self.or_calculated = False
        self.or_bars = []
        self.or_end_time = None
        
        self.current_spx_low = 1_000_000.0
        self.breakout_detected = False
        self.entry_attempted_today = False
        
        self.options_requested = False
        self.requested_strikes = []
        self.received_options = []  # Clear received options

    # =========================================================================
    # QUOTE HANDLING & PRICE TRACKING
    # =========================================================================

    def on_quote_tick(self, tick: QuoteTick):
        """Handle quote ticks - only process SPX quotes for price tracking."""
        
        # Filter: Only process quotes for the primary instrument (SPX)
        # Option quotes are handled separately for position monitoring
        if tick.instrument_id != self.instrument_id:
            return
        
        # Update SPX price
        bid = tick.bid_price.as_double()
        ask = tick.ask_price.as_double()
        
        if bid > 0 and ask > 0:
            self.current_spx_price = (bid + ask) / 2
        elif bid > 0:
            self.current_spx_price = bid
        elif ask > 0:
            self.current_spx_price = ask
        else:
            return
        
        # Update daily low
        if self.current_spx_price < self.current_spx_low:
            self.current_spx_low = self.current_spx_price
        
        # Track quote time
        self.last_quote_time_ns = tick.ts_event
        
        # Log SPX price periodically
        if int(self.clock.timestamp_ns() / 1_000_000_000) % 30 == 0:
            or_status = f"OR Low={self.or_low:.2f}" if self.or_low else "OR pending"
            self.logger.info(
                f"SPX: {self.current_spx_price:.2f}, "
                f"Today Low: {self.current_spx_low:.2f}, "
                f"{or_status}"
            )
        
        # Check for breakout if OR calculated
        if self.or_calculated and not self.breakout_detected:
            self._check_breakout()
        
        # Monitor active position for exit conditions
        if self.active_option_id:
            self._check_exit_conditions()

    # =========================================================================
    # ENTRY LOGIC
    # =========================================================================

    def _check_breakout(self):
        """Check if SPX has broken below opening range low."""
        if self.breakout_detected or self.entry_attempted_today:
            return
        
        if self.current_spx_low < self.or_low:
            self.logger.info(
                f"🔥 BREAKOUT DETECTED! SPX Low {self.current_spx_low:.2f} < "
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
            return False, "Strategy disabled"
        
        # Check market hours
        if not self.is_market_open():
            return False, "Market closed"
        
        # Check time cutoff
        if not self.is_before_cutoff():
            return False, f"Past cutoff time ({self.cutoff_time})"
        
        # Check OR calculated
        if not self.or_calculated:
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
        self._request_put_options(base_strike)
        
        self.entry_attempted_today = True

    def _calculate_target_strike(self) -> Optional[float]:
        """
        Calculate target strike price based on option price target.
        We want an option priced close to target_option_price (e.g., $4).
        
        Strategy: Request a range of strikes and select the one closest to target price.
        """
        if self.current_spx_price == 0:
            return None
        
        # We'll request multiple strikes around ATM and pick the best one
        # This is a placeholder - we'll request a range in _request_put_options
        atm_strike = round(self.current_spx_price / 5) * 5
        
        self.logger.info(
            f"Will request strike range around ATM ${atm_strike:.0f} "
            f"to find option priced near ${self.target_option_price}"
        )
        
        return atm_strike

    def _request_put_options(self, base_strike: float):
        """
        Request multiple SPX Put option contracts to find best price match.
        Requests strikes: ATM, ATM+5, ATM+10, ATM+15, ATM+20
        """
        
        # Get today's date (0DTE)
        today = self.clock.utc_now().date()
        expiry_date_ib = today.strftime("%Y%m%d")
        
        # Request 7 strikes to find the one priced closest to target
        strikes_to_request = [
            base_strike,
            base_strike - 5,
            base_strike - 10,
            base_strike - 15,
            base_strike - 20,
            base_strike - 25,
            base_strike - 30
        ]
        
        self.logger.info(
            f"Requesting SPX Puts to find option priced near ${self.target_option_price}: "
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
                    "right": "P",
                    "multiplier": "100"
                })
            
            self.request_instruments(
                venue=Venue("InteractiveBrokers"),
                params={"ib_contracts": contracts}
            )
            
            self.options_requested = True
            self.requested_strikes = strikes_to_request
            
            self.logger.info(f"✅ Requested {len(contracts)} SPX Put options")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to request Put options: {e}", exc_info=True)

    def on_instrument(self, instrument):
        """Called when new instrument is added to cache."""
        super().on_instrument(instrument)
        
        # Collect all received options for selection
        if (self.options_requested and
            not self.active_option_id and
            hasattr(instrument, 'option_kind') and
            instrument.option_kind == OptionKind.PUT):
            
            self.logger.info(f"Received option: {instrument.id}, Strike: {instrument.strike_price}")
            self.received_options.append(instrument)
            
            # Subscribe to option quotes immediately so data is available for selection
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
                alert_time=self.clock.utc_now() + timedelta(seconds=2),
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
        
        # Get quotes for all received options
        option_prices = []
        
        for option in self.received_options:
            # Subscribe to get quote
            self.subscribe_quote_ticks(option.id)
            
            # Get quote
            quote = self.cache.quote_tick(option.id)
            
            if quote:
                bid = quote.bid_price.as_double()
                ask = quote.ask_price.as_double()
                
                if bid > 0 and ask > 0:
                    mid = (bid + ask) / 2
                    spread = ask - bid
                    
                    option_prices.append({
                        'option': option,
                        'bid': bid,
                        'ask': ask,
                        'mid': mid,
                        'spread': spread
                    })
                    
                    self.logger.info(
                        f"  Strike ${float(option.strike_price.as_double()):.0f}: "
                        f"Mid=${mid:.2f}, Spread=${spread:.2f}"
                    )
        
        if not option_prices:
            self.logger.warning("No valid option quotes available")
            return
        
        # Find option with mid price closest to target (target_option_price)
        target_price = self.target_option_price
        best_option_data = min(
            option_prices,
            key=lambda x: abs(x['mid'] - target_price)
        )
        
        selected_option = best_option_data['option']
        selected_mid = best_option_data['mid']
        selected_spread = best_option_data['spread']
        
        self.logger.info(
            f"✅ Selected option: Strike ${float(selected_option.strike_price.as_double()):.0f}, "
            f"Mid=${selected_mid:.2f} (closest to target ${target_price:.2f}), "
            f"Spread=${selected_spread:.2f}"
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
        
        # Create limit order at ask price
        order = self.order_factory.limit(
            instrument_id=option.id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_int(self.quantity),
            price=Price.from_str(str(ask)),
            time_in_force=TimeInForce.DAY
        )
        
        # Submit entry order
        if self.submit_entry_order(order):
            self.active_option_id = option.id
            self.entry_price = ask
            
            # Calculate exit levels
            # Stop Loss: 40% below entry price
            self.stop_loss_price = ask * (1 - self.stop_loss_percent / 100)
            
            # Take Profit: entry + take_profit_dollars PER CONTRACT
            # Note: Option price is per share, multiply by 100 for contract value
            self.take_profit_price = ask + (self.take_profit_dollars / 100)
            
            self.logger.info(
                f"📈 ENTRY ORDER SUBMITTED: {option.id} @ ${ask:.2f} (Limit)\n"
                f"   Entry per share: ${ask:.2f}\n"
                f"   Entry per contract: ${ask * 100:.2f}\n"
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
        self.logger.info("Stopping ORB 15-Min Long Put strategy")
        
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
        return {
            "or_high": self.or_high,
            "or_low": self.or_low,
            "or_calculated": self.or_calculated,
            "last_or_calculation_date": str(self.last_or_calculation_date) if self.last_or_calculation_date else None,
            "last_reset_date": str(self.last_reset_date) if self.last_reset_date else None,
            "current_spx_low": self.current_spx_low,

            "breakout_detected": self.breakout_detected,
            "entry_attempted_today": self.entry_attempted_today,
            "active_option_id": str(self.active_option_id) if self.active_option_id else None,
            "entry_price": self.entry_price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "options_requested": self.options_requested,
            "requested_strikes": self.requested_strikes,
        }

    def set_state(self, state: Dict[str, Any]):
        """Restore strategy state."""
        self.or_high = state.get("or_high")
        self.or_low = state.get("or_low")
        self.or_calculated = state.get("or_calculated", False)
        
        if date_str:
            from datetime import datetime
            self.last_or_calculation_date = datetime.fromisoformat(date_str).date()
            
        reset_date_str = state.get("last_reset_date")
        if reset_date_str:
            from datetime import datetime
            self.last_reset_date = datetime.fromisoformat(reset_date_str).date()

        
        self.current_spx_low = state.get("current_spx_low", 1_000_000.0)
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