"""
SPX 15-Minute Range Breakout Strategy

This strategy:
1. Builds a High/Low range during the first 15 minutes of trading (09:30-09:45 ET)
2. Waits for a close below the Low to enter a Call Credit Spread
3. Invalidates entry signal if High is breached (close above High)
4. Uses minute-based candle emulation from ticks for accurate signals

Entry Logic:
- Trigger: Minute close below 15-min range Low (while High not breached)
- Instrument: Call Credit Spread (Short Call + Long Call protection)
- Short Strike: Above range High
- Long Strike: Short Strike + width (protection)

Risk Management:
- Stop Loss: 2x initial credit received
- Take Profit: Fixed dollar amount (e.g., $50 per spread)
- Trade once per day only
"""

from datetime import datetime, time, timedelta
import pytz
import math
from typing import Dict, Any, Optional

from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OptionKind, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.instruments import Instrument

from app.strategies.base_spx import SPXBaseStrategy
from app.strategies.config import StrategyConfig


class SPX15MinRangeStrategy(SPXBaseStrategy):
    """
    SPX 15-Minute Range Breakout Strategy.
    
    Works exclusively on ticks:
    1. Builds range (High/Low) from ticks during opening period
    2. Detects minute close by monitoring tick timestamp changes
    3. Enters at the START of the next minute after signal confirmation
    
    Configuration via StrategyConfig.parameters:
    - timezone: str (default "US/Eastern")
    - start_time_str: str (default "09:30:00")
    - window_minutes: int (default 15)
    - min_credit_amount: float (default 50.0) - minimum credit in dollars
    - quantity: int (default 2) - number of spreads
    - strike_width: int (default 5) - width between strikes
    - stop_loss_multiplier: float (default 2.0)
    - take_profit_amount: float (default 50.0)
    - strike_step: int (default 5)
    - signal_max_age_seconds: int (default 5)
    - max_price_deviation: float (default 10.0)
    """

    def __init__(
        self, 
        config: StrategyConfig, 
        integration_manager=None, 
        persistence_manager=None
    ):
        super().__init__(config, integration_manager, persistence_manager)
        
        # Load parameters from config
        params = config.parameters
        
        # Time settings
        timezone_str = params.get("timezone", "US/Eastern")
        self.tz = pytz.timezone(timezone_str)
        
        start_time_str = params.get("start_time_str", "09:30:00")
        t = datetime.strptime(start_time_str, "%H:%M:%S").time()
        self.start_time = t
        self.window_minutes = params.get("window_minutes", 15)
        
        # Entry parameters
        self.min_credit_amount = params.get("min_credit_amount", 50.0)
        self.config_quantity = params.get("quantity", 2)
        self.strike_width = params.get("strike_width", 5)
        
        # Risk management
        self.stop_loss_multiplier = params.get("stop_loss_multiplier", 2.0)
        self.take_profit_amount = params.get("take_profit_amount", 50.0)
        
        # Strike parameters
        self.strike_step = params.get("strike_step", 5)
        
        # Signal validation
        self.signal_max_age_seconds = params.get("signal_max_age_seconds", 5)
        self.max_price_deviation = params.get("max_price_deviation", 10.0)
        
        # Range state
        self.daily_high: Optional[float] = None
        self.daily_low: Optional[float] = None
        self.range_calculated: bool = False
        self.current_trading_day = None
        
        # Trading state
        self.high_breached: bool = False
        self.traded_today: bool = False
        self.entry_in_progress: bool = False
        
        # Spread formation state
        self._target_short_strike: Optional[float] = None
        self._target_long_strike: Optional[float] = None
        self._found_legs: Dict[float, Instrument] = {}
        self._spread_entry_price: Optional[float] = None

        # Candle emulation from ticks
        self._last_minute_idx: int = -1
        self._last_tick_price: Optional[float] = None
        
        # Signal validation
        self._signal_time: Optional[datetime] = None
        self._signal_close_price: Optional[float] = None
        
        self.logger.info(
            f"SPX15MinRangeStrategy initialized:\n"
            f"  Range Window: {self.start_time} + {self.window_minutes} min\n"
            f"  Min Credit: ${self.min_credit_amount:.2f}\n"
            f"  Strike Width: {self.strike_width}\n"
            f"  SL Multiplier: {self.stop_loss_multiplier}x\n"
            f"  TP Amount: ${self.take_profit_amount:.2f}"
        )

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    def on_start_safe(self):
        """Initialize strategy after base class setup."""
        super().on_start_safe()
        self.logger.info("SPX15MinRangeStrategy started (Tick-Only Mode with signal validation).")

    def on_spx_ready(self):
        """Callback when SPX data stream is ready."""
        self.logger.info("SPX Data Stream Ready - Range monitoring active.")

    # =========================================================================
    # TICK PROCESSING
    # =========================================================================

    def on_quote_tick(self, tick: QuoteTick):
        """
        Handle all quote ticks.
        
        1. SPX ticks go to parent -> on_spx_tick
        2. Spread ticks are processed for position management
        """
        super().on_quote_tick(tick)
        
        # Process spread ticks for position management
        if self.spread_instrument and tick.instrument_id == self.spread_instrument.id:
            self._process_spread_tick(tick)

    def on_spx_tick(self, tick: QuoteTick):
        """Main logic executed on each SPX tick."""
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

        # Minute change logic (Candle Close Emulation)
        current_minute_idx = current_time.hour * 60 + current_time.minute
        
        if self._last_minute_idx != -1 and current_minute_idx != self._last_minute_idx:
            # New minute started - previous minute closed
            if self._last_tick_price:
                self._on_minute_closed(self._last_tick_price)
        
        self._last_minute_idx = current_minute_idx
        self._last_tick_price = price

        # Calculate range window end time (09:30 + 15min = 09:45)
        end_minute = self.start_time.minute + self.window_minutes
        end_hour = self.start_time.hour + (1 if end_minute >= 60 else 0)
        end_minute = end_minute % 60
        end_time = time(end_hour, end_minute, 0)

        # 2. During range formation period (09:30-09:45)
        if self.start_time <= current_time < end_time:
            if not self.daily_high or price > self.daily_high:
                self.daily_high = price
            if not self.daily_low or price < self.daily_low:
                self.daily_low = price
            self.range_calculated = False

        # 3. Lock in range after window period
        elif current_time >= end_time and not self.range_calculated:
            if self.daily_high and self.daily_low:
                self.range_calculated = True
                self.high_breached = False
                self.logger.info(
                    f"🎯 RANGE LOCKED: High={self.daily_high:.2f}, Low={self.daily_low:.2f}"
                )
                self.save_state()

    def _on_minute_closed(self, close_price: float):
        """
        Called once at the start of a new minute.
        close_price is the last tick price of the previous minute.
        """
        if not self.range_calculated:
            return

        self.logger.debug(
            f"📊 Minute closed: {close_price:.2f} | "
            f"Range: {self.daily_low:.2f}-{self.daily_high:.2f} | "
            f"Current: {self.current_spx_price:.2f} | "
            f"Breached: {self.high_breached} | Traded: {self.traded_today}"
        )

        # 1. Check for High breach (invalidates entry)
        if close_price > self.daily_high:
            if not self.high_breached:
                self.logger.warning(
                    f"🚫 High BREACHED (Close {close_price:.2f} > {self.daily_high:.2f}). "
                    "Entry INVALIDATED for today."
                )
                self.high_breached = True
                self.save_state()

        # 2. Check entry conditions
        if (not self.high_breached and 
            not self.traded_today and 
            not self.entry_in_progress and 
            close_price < self.daily_low):
            
            # Validate signal freshness - check if price bounced back
            current_price = self.current_spx_price
            price_deviation = current_price - self.daily_low
            
            if price_deviation > self.max_price_deviation:
                self.logger.warning(
                    f"⚠️ SIGNAL IGNORED: Close={close_price:.2f} < Low={self.daily_low:.2f}, "
                    f"but current price {current_price:.2f} bounced {price_deviation:.2f} pts "
                    f"(max={self.max_price_deviation})"
                )
                return
            
            self._signal_time = self.clock.utc_now()
            self._signal_close_price = close_price
            
            self.logger.info(
                f"⚡ ENTRY SIGNAL (Minute Close): Close={close_price:.2f} < Low={self.daily_low:.2f}, "
                f"Current: {current_price:.2f} (deviation: {price_deviation:.2f} pts)"
            )
            self._initiate_entry_sequence()

    # =========================================================================
    # ENTRY SEQUENCE
    # =========================================================================

    def _initiate_entry_sequence(self):
        """Begin the entry process - find and create spread instrument."""
        self.entry_in_progress = True
        
        # Calculate strike prices
        # Short strike: just above range High
        target_short = self.daily_high + 0.01
        self._target_short_strike = math.ceil(target_short / self.strike_step) * self.strike_step
        self._target_long_strike = self._target_short_strike + self.strike_width
        
        self.logger.info(
            f"🔍 Searching for options: "
            f"Short Strike={self._target_short_strike}, Long Strike={self._target_long_strike}"
        )
        
        # Request option contracts
        today_str = self.clock.utc_now().date().strftime("%Y%m%d")
        
        contracts = []
        for strike in [self._target_short_strike, self._target_long_strike]:
            contracts.append({
                "secType": "OPT",
                "symbol": "SPX",
                "tradingClass": "SPXW",
                "exchange": "CBOE",
                "currency": "USD",
                "lastTradeDateOrContractMonth": today_str,
                "strike": float(strike),
                "right": "C",
                "multiplier": "100"
            })

        self.request_instruments(
            venue=Venue("InteractiveBrokers"),
            params={"ib_contracts": contracts}
        )
        
        # Set timeout for entry process
        self.clock.set_time_alert(
            name=f"{self.id}_entry_timeout",
            alert_time=self.clock.utc_now() + timedelta(seconds=30),
            callback=self._on_entry_timeout
        )

    def on_instrument(self, instrument: Instrument):
        """Handle received instruments - track option legs."""
        super().on_instrument(instrument)
        
        if not self.entry_in_progress:
            return

        # Check if this is an option we're looking for
        if hasattr(instrument, 'strike_price') and hasattr(instrument, 'option_kind'):
            strike = float(instrument.strike_price.as_double())
            
            is_target = False
            if strike == self._target_short_strike and instrument.option_kind == OptionKind.CALL:
                is_target = True
            elif strike == self._target_long_strike and instrument.option_kind == OptionKind.CALL:
                is_target = True
                
            if is_target:
                self.logger.info(f"✅ Found leg: {instrument.id} (Strike {strike})")
                self._found_legs[strike] = instrument
                
                # Check if we have both legs
                if len(self._found_legs) >= 2:
                    self._create_spread_instrument()

    def _create_spread_instrument(self):
        """Create the spread instrument from found legs."""
        short_inst = self._found_legs[self._target_short_strike]
        long_inst = self._found_legs[self._target_long_strike]
        
        # Define legs: Buy protection (long), Sell for credit (short)
        # For a Credit Spread: we BUY the spread instrument (which sells the debit side)
        legs = [
            (long_inst.id, 1),   # Buy the long strike (protection)
            (short_inst.id, -1)  # Sell the short strike (credit)
        ]
        
        self.logger.info("Creating spread instrument...")
        self.create_and_request_spread(legs)

    def on_spread_ready(self, instrument: Instrument):
        """Called when spread instrument is available."""
        self.logger.info(f"Spread ready: {instrument.id}. Waiting for quote...")
        # Entry will happen in _process_spread_tick when we get a quote

    def _process_spread_tick(self, tick: QuoteTick):
        """Process spread ticks for entry and position management."""
        if self.entry_in_progress and not self.traded_today:
            self._check_and_submit_entry(tick)
        elif self.get_effective_spread_quantity() != 0:
            self._manage_open_position()

    def _check_and_submit_entry(self, quote: QuoteTick):
        """Check spread price and submit entry if conditions are met."""
        bid = quote.bid_price.as_double()
        ask = quote.ask_price.as_double()
        mid = (bid + ask) / 2
        
        # For a credit spread sold as BUY order:
        # We receive credit when we BUY (because short leg > long leg value)
        # Credit received = abs(mid) when mid is negative
        target_price = -(self.min_credit_amount / 100.0)
        
        # Validate signal freshness
        if self._signal_time:
            signal_age = (self.clock.utc_now() - self._signal_time).total_seconds()
            
            if signal_age > self.signal_max_age_seconds:
                self.logger.warning(
                    f"⚠️ SIGNAL EXPIRED: {signal_age:.1f}s elapsed. Entry cancelled."
                )
                self._cancel_entry()
                return
            
            # Check if SPX bounced away from Low
            price_deviation = self.current_spx_price - self.daily_low
            if price_deviation > self.max_price_deviation:
                self.logger.warning(
                    f"⚠️ SPX bounced from Low: {self.current_spx_price:.2f}, "
                    f"deviation {price_deviation:.2f} > max {self.max_price_deviation}. Entry cancelled."
                )
                self._cancel_entry()
                return
        
        # Check if we can get enough credit
        # mid should be negative for credit spread, and more negative = more credit
        if mid <= target_price:
            signal_age = (self.clock.utc_now() - self._signal_time).total_seconds() if self._signal_time else 0
            
            self.logger.info(
                f"✅ Price acceptable ({mid:.4f} <= {target_price:.4f}). "
                f"Entering (signal age: {signal_age:.1f}s)."
            )
            
            self.open_spread_position(
                quantity=self.config_quantity,
                is_buy=True,
                limit_price=mid
            )
            
            self.traded_today = True
            self.entry_in_progress = False
            self._spread_entry_price = abs(mid)  # Store as positive credit amount
            self._signal_time = None
            self._signal_close_price = None
            self.save_state()

    def _cancel_entry(self):
        """Cancel the entry process and clean up."""
        self.entry_in_progress = False
        self._signal_time = None
        self._signal_close_price = None

    def _manage_open_position(self):
        """Monitor open position for stop loss and take profit."""
        if self._spread_entry_price is None:
            return

        quote = self.cache.quote_tick(self.spread_instrument.id)
        if not quote:
            return

        mid = (quote.bid_price.as_double() + quote.ask_price.as_double()) / 2
        
        # STOP LOSS
        # If mid becomes more negative (spread costs more to buy back), we're losing
        stop_price = -(self._spread_entry_price * self.stop_loss_multiplier)
        if mid <= stop_price:
            self.logger.warning(
                f"🛑 STOP LOSS: Price {mid:.4f} hit stop {stop_price:.4f}. Closing."
            )
            self.close_spread_smart()
            self._spread_entry_price = None
            return

        # TAKE PROFIT
        # We want the spread to become LESS negative (cheaper to buy back)
        tp_points = self.take_profit_amount / 100.0
        required_debit = self._spread_entry_price - tp_points
        if required_debit < 0.05:
            required_debit = 0.05
        tp_price = -required_debit
        
        if mid >= tp_price:
            self.logger.info(
                f"💰 TAKE PROFIT: Price {mid:.4f} hit target {tp_price:.4f}. Closing."
            )
            self.close_spread_smart()
            self._spread_entry_price = None

    def _on_entry_timeout(self, event):
        """Handle entry timeout."""
        if self.entry_in_progress:
            if self.spread_instrument:
                self.logger.info("⏱️ Timeout, but spread is ready. Waiting for quote...")
            else:
                self.logger.warning("⏱️ Entry Timeout. Spread not ready. Cancelling.")
                self._cancel_entry()

    # =========================================================================
    # STATE MANAGEMENT
    # =========================================================================

    def _reset_daily_state(self, new_date):
        """Reset all daily state for new trading day."""
        
        self.daily_high = None
        self.daily_low = None
        self.range_calculated = False
        self.current_trading_day = new_date
        
        self.high_breached = False
        self.traded_today = False
        self.entry_in_progress = False
        self._found_legs.clear()
        self._spread_entry_price = None
        self._last_minute_idx = -1
        self._last_tick_price = None
        self._signal_time = None
        self._signal_close_price = None
        
        self.logger.info(f"📅 Daily state reset for {new_date}")

    def get_state(self) -> Dict[str, Any]:
        """Return strategy-specific state for persistence."""
        state = super().get_state()
        state.update({
            "daily_high": self.daily_high,
            "daily_low": self.daily_low,
            "range_calculated": self.range_calculated,
            "current_trading_day": str(self.current_trading_day) if self.current_trading_day else None,
            "high_breached": self.high_breached,
            "traded_today": self.traded_today,
            "_spread_entry_price": self._spread_entry_price,
            "_target_short_strike": self._target_short_strike,
            "_target_long_strike": self._target_long_strike,
        })
        return state

    def set_state(self, state: Dict[str, Any]):
        """Restore strategy-specific state."""
        super().set_state(state)
        
        self.daily_high = state.get("daily_high")
        self.daily_low = state.get("daily_low")
        self.range_calculated = state.get("range_calculated", False)
        
        trading_day_str = state.get("current_trading_day")
        if trading_day_str:
            self.current_trading_day = datetime.strptime(trading_day_str, "%Y-%m-%d").date()
        
        self.high_breached = state.get("high_breached", False)
        self.traded_today = state.get("traded_today", False)
        self._spread_entry_price = state.get("_spread_entry_price")
        self._target_short_strike = state.get("_target_short_strike")
        self._target_long_strike = state.get("_target_long_strike")
        
        self.logger.info(
            f"State restored: Range={self.daily_low}-{self.daily_high}, "
            f"Calculated={self.range_calculated}, Traded={self.traded_today}"
        )

    def on_stop_safe(self):
        """Clean up when strategy stops."""
        # Close any open positions
        if self.get_effective_spread_quantity() != 0:
            self.logger.info("Closing spread position on strategy stop...")
            self.close_spread_smart()
        
        super().on_stop_safe()
        self.logger.info("SPX15MinRangeStrategy stopped.")
