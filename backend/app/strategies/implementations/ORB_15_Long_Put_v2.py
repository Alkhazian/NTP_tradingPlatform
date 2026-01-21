"""
ORB 15-Minute Long Put Strategy V2

Opening Range Breakout strategy that inherits from SPXBaseStrategy.
Uses find_option_by_premium() for standardized option search.

Key features:
1. Calculates opening range during configurable period (default: 9:30-9:45 AM ET)
2. Enters Long Put when SPX breaks below opening range low
3. Entry conditions: Market open, before cutoff time (configurable), SPX < OR Low
4. Position: SPX Put, 0DTE, strike selected by target option price (configurable)
5. Risk management: Configurable stop loss %, configurable take profit $
6. Entry validation: Bid/ask spread < configurable max spread (via find_option_by_premium)

Changes from V1:
- Inherits from SPXBaseStrategy (not BaseStrategy)
- Uses find_option_by_premium() with callback pattern for option search
- Automatic unsubscription from non-selected options (handled by base class)
- Manual unsubscription from selected option on position close
"""

from typing import Dict, Any, Optional
from datetime import datetime, time, timedelta
import pytz
from decimal import Decimal

from nautilus_trader.model.data import QuoteTick, Bar, BarType, BarSpecification
from nautilus_trader.model.enums import OrderSide, TimeInForce, OptionKind
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity, Price
from nautilus_trader.model.instruments import Instrument

from app.strategies.base_spx import SPXBaseStrategy
from app.strategies.config import StrategyConfig


class Orb15MinLongPutV2Strategy(SPXBaseStrategy):
    """
    Opening Range Breakout 15-Minute Long Put Strategy V2.
    
    Trades SPX 0DTE Put options based on opening range breakout.
    Uses SPXBaseStrategy for standardized SPX subscription and option search.
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
        
        # State tracking for logging
        self._last_price_logged_time = 0
        
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

        
        # SPX price tracking (current_spx_price inherited from SPXBaseStrategy)
        self.current_spx_low = 1_000_000.0  # Today's low, init to high number
        self.last_quote_time_ns = 0
        
        # Position management
        self._selected_option_id: Optional[InstrumentId] = None
        self.entry_price = None
        self.stop_loss_price = None
        self.take_profit_price = None
        
        # Entry tracking
        self.breakout_detected = False
        self.entry_attempted_today = False
        
        # Search tracking
        self._active_search_id: Optional[str] = None

    # =========================================================================
    # SPXBaseStrategy ABSTRACT METHOD IMPLEMENTATIONS
    # =========================================================================

    def on_spx_ready(self):
        """Called when SPX subscription is ready and data is flowing."""
        self.logger.info(
            f"✅ ORB 15-Min Long Put V2 Strategy ready: "
            f"OR={self.opening_range_minutes}m, "
            f"Target option price=${self.target_option_price}, "
            f"SL={self.stop_loss_percent}%, "
            f"TP=${self.take_profit_dollars} per contract, "
            f"Max spread=${self.max_spread_dollars}"
        )
        
        # Subscribe to 1-minute bars for opening range calculation
        self._subscribe_to_bars()

    def on_spx_tick(self, tick: QuoteTick):
        """Called for each SPX quote tick."""
        # Reset daily state if new day
        if self.should_reset_daily_state():
            self._reset_daily_state()
        
        # Update daily low using mid price from base class
        if self.current_spx_price < self.current_spx_low:
            self.current_spx_low = self.current_spx_price
        
        # Track quote time
        self.last_quote_time_ns = tick.ts_event
        
        # Log SPX price periodically (every 30s)
        now_sec = int(self.clock.timestamp_ns() / 1_000_000_000)
        if now_sec % 30 == 0 and now_sec != self._last_price_logged_time:
            self._last_price_logged_time = now_sec
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
        if self._selected_option_id:
            self._check_exit_conditions()

    # =========================================================================
    # BAR SUBSCRIPTION & HANDLING
    # =========================================================================

    def _subscribe_to_bars(self):
        """Subscribe to 1-minute bars for opening range calculation."""
        try:
            bar_type = BarType(
                self.spx_instrument_id,
                BarSpecification.from_str("1-MINUTE-LAST")
            )
            self.subscribe_bars(bar_type)
            self.logger.info(f"Subscribed to 1-minute bars for OR calculation")
        except Exception as e:
            self.logger.error(f"Failed to subscribe to bars: {e}", exc_info=True)

    def on_bar_safe(self, bar: Bar):
        """Handle 1-minute bars for opening range calculation."""
        
        # Reset daily state if new day
        if self.should_reset_daily_state():
            self._reset_daily_state()
        
        # Update current low from bar
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
            f"📈 Opening Range calculated ({self.opening_range_minutes}m): "
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
        
        self._active_search_id = None

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
        """Prepare for entry by searching for Put option via find_option_by_premium."""
        self.logger.info("Preparing entry - searching for Put option")
        
        self.entry_attempted_today = True
        
        # Use SPXBaseStrategy's find_option_by_premium with callback
        search_id = self.find_option_by_premium(
            target_premium=self.target_option_price,
            option_kind=OptionKind.PUT,
            max_spread=self.max_spread_dollars,
            selection_delay_seconds=self.DEFAULT_SELECTION_DELAY_SECONDS,
            callback=self._on_put_option_found
        )
        
        if search_id:
            self._active_search_id = search_id
            self.logger.info(f"Option search initiated (search_id: {search_id[:8]}...)")
        else:
            self.logger.error("Failed to initiate option search")

    def _on_put_option_found(
        self, 
        search_id: str, 
        option: Optional[Instrument], 
        option_data: Optional[Dict]
    ):
        """
        Callback when find_option_by_premium completes.
        
        NOTE: At this point, base class has ALREADY unsubscribed from all
        non-selected options. Only the selected option (if any) remains subscribed.
        
        Args:
            search_id: Search ID that completed
            option: Selected option instrument (None if search failed)
            option_data: Price data for selected option (None if search failed)
        """
        # Verify this is our active search
        if search_id != self._active_search_id:
            self.logger.warning(f"Received callback for unknown search: {search_id[:8]}...")
            return
        
        self._active_search_id = None
        
        if option is None:
            self.logger.warning(
                "❌ No suitable Put option found - entry skipped for today\n"
                "   (No retries - this was the only entry attempt)"
            )
            return
        
        self.logger.info(
            f"✅ Put option found: Strike ${float(option.strike_price.as_double()):.0f}, "
            f"Mid=${option_data['mid']:.2f}, Spread=${option_data['spread']:.2f}"
        )
        
        # Try to enter with selected option
        self._try_entry_with_option(option, option_data)

    def _try_entry_with_option(self, option: Instrument, option_data: Dict):
        """
        Attempt to enter position with the selected option contract.
        
        Args:
            option: Selected option instrument
            option_data: Quote data (bid, ask, mid, spread)
        """
        ask = option_data['ask']
        
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
            self._selected_option_id = option.id
            self.entry_price = ask
            
            # Calculate exit levels
            # Stop Loss: X% below entry price
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
            
            self.save_state()


    # =========================================================================
    # EXIT LOGIC
    # =========================================================================

    def _check_exit_conditions(self):
        """Monitor position and check for exit conditions (SL/TP)."""
        if not self._selected_option_id:
            return
        
        # Get current option quote
        quote = self.cache.quote_tick(self._selected_option_id)
        
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
        """Exit the position and unsubscribe from selected option."""
        if not self._selected_option_id:
            return
        
        self.logger.info(f"Exiting position: {reason}")
        
        # Store option ID before clearing (for unsubscription)
        option_id_to_unsubscribe = self._selected_option_id
        
        # Use base method to close position
        self.close_strategy_position(reason=reason)
        
        # CRITICAL: Unsubscribe from the selected option (cleanup)
        try:
            self.unsubscribe_quote_ticks(option_id_to_unsubscribe)
            self.logger.info(f"🧹 Unsubscribed from {option_id_to_unsubscribe}")
        except Exception as e:
            self.logger.warning(f"Failed to unsubscribe from option: {e}")
        
        # Reset state
        self._selected_option_id = None
        self.entry_price = None
        self.stop_loss_price = None
        self.take_profit_price = None
        
        self.save_state()

    # =========================================================================
    # LIFECYCLE CLEANUP
    # =========================================================================

    def on_stop_safe(self):
        """Called when strategy stops."""
        self.logger.info("Stopping ORB 15-Min Long Put V2 strategy")
        
        # Cancel any active option search
        if self._active_search_id:
            self.cancel_premium_search(self._active_search_id)
            self._active_search_id = None
        
        # Close any open positions
        if self._has_open_position():
            self.close_strategy_position(reason="STRATEGY_STOP")
        
        # Unsubscribe from selected option if subscribed
        if self._selected_option_id:
            try:
                self.unsubscribe_quote_ticks(self._selected_option_id)
            except Exception as e:
                self.logger.warning(f"Failed to unsubscribe from option: {e}")
            self._selected_option_id = None
        
        # Let parent handle SPX unsubscription
        super().on_stop_safe()

    # =========================================================================
    # STATE PERSISTENCE
    # =========================================================================

    def get_state(self) -> Dict[str, Any]:
        """Return strategy state for persistence."""
        state = super().get_state()
        state.update({
            "or_high": self.or_high,
            "or_low": self.or_low,
            "or_calculated": self.or_calculated,
            "last_or_calculation_date": str(self.last_or_calculation_date) if self.last_or_calculation_date else None,
            "last_reset_date": str(self.last_reset_date) if self.last_reset_date else None,
            "current_spx_low": self.current_spx_low,

            "breakout_detected": self.breakout_detected,
            "entry_attempted_today": self.entry_attempted_today,
            "selected_option_id": str(self._selected_option_id) if self._selected_option_id else None,
            "entry_price": self.entry_price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
        })
        return state

    def set_state(self, state: Dict[str, Any]):
        """Restore strategy state."""
        super().set_state(state)
        
        self.or_high = state.get("or_high")
        self.or_low = state.get("or_low")
        self.or_calculated = state.get("or_calculated", False)
        
        date_str = state.get("last_or_calculation_date")
        if date_str:
            self.last_or_calculation_date = datetime.fromisoformat(date_str).date()
            
        reset_date_str = state.get("last_reset_date")
        if reset_date_str:
            self.last_reset_date = datetime.fromisoformat(reset_date_str).date()

        
        self.current_spx_low = state.get("current_spx_low", 1_000_000.0)
        self.breakout_detected = state.get("breakout_detected", False)
        self.entry_attempted_today = state.get("entry_attempted_today", False)
        
        option_id_str = state.get("selected_option_id")
        if option_id_str:
            self._selected_option_id = InstrumentId.from_str(option_id_str)
            # Re-subscribe to option for monitoring
            try:
                self.subscribe_quote_ticks(self._selected_option_id)
            except Exception as e:
                self.logger.warning(f"Failed to resubscribe to option: {e}")
        
        self.entry_price = state.get("entry_price")
        self.stop_loss_price = state.get("stop_loss_price")
        self.take_profit_price = state.get("take_profit_price")
        
        self.logger.info(
            f"State restored: OR={self.or_calculated}, "
            f"breakout={self.breakout_detected}, "
            f"option={self._selected_option_id}"
        )
