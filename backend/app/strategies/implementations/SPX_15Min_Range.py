"""
SPX 15-Minute Range Breakout Strategy (Bidirectional)

This strategy:
1. Builds a High/Low range during the first 15 minutes of trading (09:30-09:45 ET)
2. Trades breakouts in BOTH directions with cross-invalidation:
   - Bearish: Close below Low → Call Credit Spread (if High not breached first)
   - Bullish: Close above High → Put Credit Spread (if Low not breached first)
3. Uses minute-based candle emulation from ticks for accurate signals

Entry Logic:
- Bearish Trigger: Minute close below Low (while High not breached)
  - Instrument: Call Credit Spread (Short Call + Long Call protection)
  - Short Strike: Above range High
  - Long Strike: Short Strike + width (protection)
  
- Bullish Trigger: Minute close above High (while Low not breached)
  - Instrument: Put Credit Spread (Short Put + Long Put protection)
  - Short Strike: Below range Low
  - Long Strike: Short Strike - width (protection)

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
        
        # Trading state - bidirectional breach tracking
        self.high_breached: bool = False
        self.low_breached: bool = False
        self.traded_today: bool = False
        self.entry_in_progress: bool = False
        
        # Spread formation state
        self._target_short_strike: Optional[float] = None
        self._target_long_strike: Optional[float] = None
        self._found_legs: Dict[float, Instrument] = {}
        self._spread_entry_price: Optional[float] = None
        self._signal_direction: Optional[str] = None  # 'bearish' or 'bullish'

        # Candle emulation from ticks
        self._last_minute_idx: int = -1
        self._last_tick_price: Optional[float] = None
        
        # Signal validation
        self._signal_time: Optional[datetime] = None
        self._signal_close_price: Optional[float] = None
        
        # Diagnostic counters
        self._tick_count: int = 0
        self._range_tick_count: int = 0
        self._last_log_minute: int = -1
        
        self.logger.info(
            f"═══════════════════════════════════════════════════════════════\n"
            f"SPX15MinRangeStrategy INITIALIZED\n"
            f"═══════════════════════════════════════════════════════════════\n"
            f"  📅 Range Window: {self.start_time} + {self.window_minutes} min\n"
            f"  💰 Min Credit: ${self.min_credit_amount:.2f}\n"
            f"  📏 Strike Width: {self.strike_width} pts\n"
            f"  🛡️ Strike Step: {self.strike_step} pts\n"
            f"  🛑 SL Multiplier: {self.stop_loss_multiplier}x\n"
            f"  💵 TP Amount: ${self.take_profit_amount:.2f}\n"
            f"  ⏱️ Signal Max Age: {self.signal_max_age_seconds}s\n"
            f"  📊 Max Price Deviation: {self.max_price_deviation} pts\n"
            f"  📦 Quantity: {self.config_quantity} spreads\n"
            f"═══════════════════════════════════════════════════════════════"
        )

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    def on_start_safe(self):
        """Initialize strategy after base class setup."""
        super().on_start_safe()
        self.logger.info(
            f"🚀 SPX15MinRangeStrategy STARTED\n"
            f"   Mode: Tick-Only with Bidirectional Breakout\n"
            f"   Waiting for SPX data stream..."
        )

    def on_spx_ready(self):
        """Callback when SPX data stream is ready."""
        self.logger.info(
            f"✅ SPX DATA STREAM READY\n"
            f"   Current SPX Price: {self.current_spx_price:.2f}\n"
            f"   Range monitoring is now ACTIVE"
        )

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
        self._tick_count += 1
        
        utc_now = self.clock.utc_now()
        et_now = utc_now.astimezone(self.tz)
        current_date = et_now.date()
        current_time = et_now.time()
        price = self.current_spx_price

        if price <= 0:
            self.logger.warning(f"⚠️ Invalid SPX price received: {price}. Tick ignored.")
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
            self._range_tick_count += 1
            old_high = self.daily_high
            old_low = self.daily_low
            
            if not self.daily_high or price > self.daily_high:
                self.daily_high = price
            if not self.daily_low or price < self.daily_low:
                self.daily_low = price
            self.range_calculated = False
            
            # Log range updates periodically (every minute)
            if current_minute_idx != self._last_log_minute:
                self._last_log_minute = current_minute_idx
                self.logger.info(
                    f"📈 RANGE FORMING [{current_time.strftime('%H:%M')}]: "
                    f"High={self.daily_high:.2f}, Low={self.daily_low:.2f}, "
                    f"Width={self.daily_high - self.daily_low:.2f} pts, "
                    f"Ticks={self._range_tick_count}"
                )

        # 3. Lock in range after window period
        elif current_time >= end_time and not self.range_calculated:
            if self.daily_high and self.daily_low:
                range_width = self.daily_high - self.daily_low
                self.range_calculated = True
                self.high_breached = False
                self.low_breached = False
                self.logger.info(
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"🎯 RANGE LOCKED at {current_time.strftime('%H:%M:%S')} ET\n"
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"   📊 High: {self.daily_high:.2f}\n"
                    f"   📊 Low:  {self.daily_low:.2f}\n"
                    f"   📏 Width: {range_width:.2f} pts\n"
                    f"   📈 Range Ticks: {self._range_tick_count}\n"
                    f"   🔴 BEARISH trigger: Close < {self.daily_low:.2f}\n"
                    f"   🟢 BULLISH trigger: Close > {self.daily_high:.2f}\n"
                    f"═══════════════════════════════════════════════════════════════"
                )
                self.save_state()
            else:
                self.logger.error(
                    f"❌ RANGE LOCK FAILED at {current_time.strftime('%H:%M:%S')}: "
                    f"High={self.daily_high}, Low={self.daily_low}. "
                    f"Insufficient data during range window!"
                )

    def _on_minute_closed(self, close_price: float):
        """
        Called once at the start of a new minute.
        close_price is the last tick price of the previous minute.
        Handles bidirectional breakout detection.
        """
        if not self.range_calculated:
            self.logger.debug(f"Minute closed at {close_price:.2f} but range not yet calculated. Skipping.")
            return

        et_now = self.clock.utc_now().astimezone(self.tz)
        
        # Log every minute close with full context
        self.logger.info(
            f"⏰ MINUTE CLOSE [{et_now.strftime('%H:%M')}]: Price={close_price:.2f} | "
            f"Range=[{self.daily_low:.2f}-{self.daily_high:.2f}] | "
            f"vs Low: {close_price - self.daily_low:+.2f} | vs High: {close_price - self.daily_high:+.2f} | "
            f"State: HighBreached={self.high_breached}, LowBreached={self.low_breached}, Traded={self.traded_today}"
        )

        # 1. Check for breach conditions (cross-invalidation)
        if close_price > self.daily_high:
            if not self.high_breached:
                self.logger.warning(
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"🚫 HIGH BREACHED at {et_now.strftime('%H:%M:%S')}\n"
                    f"   Close: {close_price:.2f} > High: {self.daily_high:.2f}\n"
                    f"   → BEARISH entry is now INVALIDATED for today\n"
                    f"   → BULLISH entry remains: {'VALID' if not self.low_breached else 'INVALID'}\n"
                    f"═══════════════════════════════════════════════════════════════"
                )
                self.high_breached = True
                self.save_state()
                
        if close_price < self.daily_low:
            if not self.low_breached:
                self.logger.warning(
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"🚫 LOW BREACHED at {et_now.strftime('%H:%M:%S')}\n"
                    f"   Close: {close_price:.2f} < Low: {self.daily_low:.2f}\n"
                    f"   → BULLISH entry is now INVALIDATED for today\n"
                    f"   → BEARISH entry remains: {'VALID' if not self.high_breached else 'INVALID'}\n"
                    f"═══════════════════════════════════════════════════════════════"
                )
                self.low_breached = True
                self.save_state()

        # Skip if already traded or entry in progress
        if self.traded_today:
            self.logger.debug(f"Already traded today. Skipping signal check.")
            return
        if self.entry_in_progress:
            self.logger.debug(f"Entry already in progress. Skipping signal check.")
            return

        # 2. Check BEARISH entry (close below Low, High not breached first)
        if close_price < self.daily_low:
            if self.high_breached:
                self.logger.info(
                    f"📉 Close below Low ({close_price:.2f} < {self.daily_low:.2f}) "
                    f"but High was breached earlier. BEARISH entry BLOCKED."
                )
            else:
                current_price = self.current_spx_price
                price_deviation = current_price - self.daily_low
                
                if price_deviation > self.max_price_deviation:
                    self.logger.warning(
                        f"═══════════════════════════════════════════════════════════════\n"
                        f"⚠️ BEARISH SIGNAL REJECTED - Price Bounce\n"
                        f"═══════════════════════════════════════════════════════════════\n"
                        f"   Close: {close_price:.2f} < Low: {self.daily_low:.2f} ✓\n"
                        f"   But current price: {current_price:.2f}\n"
                        f"   Deviation from Low: {price_deviation:.2f} pts\n"
                        f"   Max allowed: {self.max_price_deviation} pts\n"
                        f"   → Entry CANCELLED due to price bounce\n"
                        f"═══════════════════════════════════════════════════════════════"
                    )
                    return
                
                self._signal_time = self.clock.utc_now()
                self._signal_close_price = close_price
                self._signal_direction = 'bearish'
                
                self.logger.info(
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"⚡ BEARISH ENTRY SIGNAL CONFIRMED\n"
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"   Trigger: Close {close_price:.2f} < Low {self.daily_low:.2f}\n"
                    f"   Current price: {current_price:.2f}\n"
                    f"   Deviation: {price_deviation:.2f} pts (max: {self.max_price_deviation})\n"
                    f"   High was NOT breached first: ✓\n"
                    f"   → Initiating CALL Credit Spread entry\n"
                    f"═══════════════════════════════════════════════════════════════"
                )
                self._initiate_entry_sequence()
                return

        # 3. Check BULLISH entry (close above High, Low not breached first)
        if close_price > self.daily_high:
            if self.low_breached:
                self.logger.info(
                    f"📈 Close above High ({close_price:.2f} > {self.daily_high:.2f}) "
                    f"but Low was breached earlier. BULLISH entry BLOCKED."
                )
            else:
                current_price = self.current_spx_price
                price_deviation = self.daily_high - current_price
                
                if price_deviation > self.max_price_deviation:
                    self.logger.warning(
                        f"═══════════════════════════════════════════════════════════════\n"
                        f"⚠️ BULLISH SIGNAL REJECTED - Price Drop\n"
                        f"═══════════════════════════════════════════════════════════════\n"
                        f"   Close: {close_price:.2f} > High: {self.daily_high:.2f} ✓\n"
                        f"   But current price: {current_price:.2f}\n"
                        f"   Deviation from High: {price_deviation:.2f} pts\n"
                        f"   Max allowed: {self.max_price_deviation} pts\n"
                        f"   → Entry CANCELLED due to price drop\n"
                        f"═══════════════════════════════════════════════════════════════"
                    )
                    return
                
                self._signal_time = self.clock.utc_now()
                self._signal_close_price = close_price
                self._signal_direction = 'bullish'
                
                self.logger.info(
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"⚡ BULLISH ENTRY SIGNAL CONFIRMED\n"
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"   Trigger: Close {close_price:.2f} > High {self.daily_high:.2f}\n"
                    f"   Current price: {current_price:.2f}\n"
                    f"   Deviation: {price_deviation:.2f} pts (max: {self.max_price_deviation})\n"
                    f"   Low was NOT breached first: ✓\n"
                    f"   → Initiating PUT Credit Spread entry\n"
                    f"═══════════════════════════════════════════════════════════════"
                )
                self._initiate_entry_sequence()

    # =========================================================================
    # ENTRY SEQUENCE
    # =========================================================================

    def _initiate_entry_sequence(self):
        """
        Begin the entry process - find and create spread instrument.
        Handles both bearish (Call Credit Spread) and bullish (Put Credit Spread) directions.
        """
        self.entry_in_progress = True
        self._found_legs.clear()  # Reset found legs
        
        # Calculate strike prices based on direction
        today_str = self.clock.utc_now().date().strftime("%Y%m%d")
        
        if self._signal_direction == 'bearish':
            # CALL CREDIT SPREAD: Short strike above High, Long strike higher
            target_short = self.daily_high + 0.01
            self._target_short_strike = math.ceil(target_short / self.strike_step) * self.strike_step
            self._target_long_strike = self._target_short_strike + self.strike_width
            option_right = "C"
            spread_type = "CALL Credit Spread"
        else:  # bullish
            # PUT CREDIT SPREAD: Short strike below Low, Long strike lower
            target_short = self.daily_low - 0.01
            self._target_short_strike = math.floor(target_short / self.strike_step) * self.strike_step
            self._target_long_strike = self._target_short_strike - self.strike_width
            option_right = "P"
            spread_type = "PUT Credit Spread"
        
        self.logger.info(
            f"═══════════════════════════════════════════════════════════════\n"
            f"🔍 INITIATING ENTRY SEQUENCE\n"
            f"═══════════════════════════════════════════════════════════════\n"
            f"   Direction: {self._signal_direction.upper()}\n"
            f"   Spread Type: {spread_type}\n"
            f"   Expiry: {today_str}\n"
            f"   Short Leg: {self._target_short_strike}{option_right} (SELL)\n"
            f"   Long Leg:  {self._target_long_strike}{option_right} (BUY)\n"
            f"   Width: {abs(self._target_long_strike - self._target_short_strike)} pts\n"
            f"   Requesting instruments from IB...\n"
            f"═══════════════════════════════════════════════════════════════"
        )
        
        # Request option contracts
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
                "right": option_right,
                "multiplier": "100"
            })

        self.request_instruments(
            venue=Venue("InteractiveBrokers"),
            params={"ib_contracts": contracts}
        )
        self.logger.debug(f"Requested {len(contracts)} option contracts from IB")
        
        # Set timeout for entry process
        self.clock.set_time_alert(
            name=f"{self.id}_entry_timeout",
            alert_time=self.clock.utc_now() + timedelta(seconds=30),
            callback=self._on_entry_timeout
        )
        self.logger.debug(f"Entry timeout set for 30 seconds")

    def on_instrument(self, instrument: Instrument):
        """Handle received instruments - track option legs for both directions."""
        super().on_instrument(instrument)
        
        if not self.entry_in_progress:
            return

        # Determine expected option kind based on direction
        expected_kind = OptionKind.CALL if self._signal_direction == 'bearish' else OptionKind.PUT

        # Check if this is an option we're looking for
        if hasattr(instrument, 'strike_price') and hasattr(instrument, 'option_kind'):
            strike = float(instrument.strike_price.as_double())
            
            is_target = False
            if strike == self._target_short_strike and instrument.option_kind == expected_kind:
                is_target = True
            elif strike == self._target_long_strike and instrument.option_kind == expected_kind:
                is_target = True
                
            if is_target:
                kind_str = "C" if expected_kind == OptionKind.CALL else "P"
                self.logger.info(f"✅ Found leg: {instrument.id} (Strike {strike}{kind_str})")
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
        
        self.logger.info(
            f"📦 Creating spread instrument...\n"
            f"   Long leg (BUY):  {long_inst.id}\n"
            f"   Short leg (SELL): {short_inst.id}"
        )
        self.create_and_request_spread(legs)

    def on_spread_ready(self, instrument: Instrument):
        """Called when spread instrument is available."""
        self.logger.info(
            f"✅ SPREAD INSTRUMENT READY\n"
            f"   ID: {instrument.id}\n"
            f"   Waiting for quote to validate entry price..."
        )
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
        spread_width = ask - bid
        
        # For a credit spread sold as BUY order:
        # We receive credit when we BUY (because short leg > long leg value)
        # Credit received = abs(mid) when mid is negative
        target_price = -(self.min_credit_amount / 100.0)
        credit_received = abs(mid) * 100 if mid < 0 else 0
        
        self.logger.debug(
            f"Spread quote: Bid={bid:.4f}, Ask={ask:.4f}, Mid={mid:.4f}, "
            f"Spread={spread_width:.4f}, Credit=${credit_received:.2f}"
        )
        
        # Validate signal freshness
        if self._signal_time:
            signal_age = (self.clock.utc_now() - self._signal_time).total_seconds()
            
            if signal_age > self.signal_max_age_seconds:
                self.logger.warning(
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"⚠️ ENTRY CANCELLED - Signal Expired\n"
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"   Signal age: {signal_age:.1f}s\n"
                    f"   Max allowed: {self.signal_max_age_seconds}s\n"
                    f"   → Entry cancelled\n"
                    f"═══════════════════════════════════════════════════════════════"
                )
                self._cancel_entry()
                return
            
            # Check if SPX bounced away from entry level
            if self._signal_direction == 'bearish':
                price_deviation = self.current_spx_price - self.daily_low
                level_name = "Low"
            else:  # bullish
                price_deviation = self.daily_high - self.current_spx_price
                level_name = "High"
                
            if price_deviation > self.max_price_deviation:
                self.logger.warning(
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"⚠️ ENTRY CANCELLED - SPX Price Bounce\n"
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"   SPX bounced from {level_name}: {self.current_spx_price:.2f}\n"
                    f"   Deviation: {price_deviation:.2f} pts\n"
                    f"   Max allowed: {self.max_price_deviation} pts\n"
                    f"   → Entry cancelled\n"
                    f"═══════════════════════════════════════════════════════════════"
                )
                self._cancel_entry()
                return
        
        # Check if we can get enough credit
        # mid should be negative for credit spread, and more negative = more credit
        if mid <= target_price:
            signal_age = (self.clock.utc_now() - self._signal_time).total_seconds() if self._signal_time else 0
            
            self.logger.info(
                f"═══════════════════════════════════════════════════════════════\n"
                f"✅ ENTRY ORDER SUBMITTED\n"
                f"═══════════════════════════════════════════════════════════════\n"
                f"   Direction: {self._signal_direction.upper()}\n"
                f"   Spread: {self.spread_instrument.id if self.spread_instrument else 'N/A'}\n"
                f"   Quantity: {self.config_quantity}\n"
                f"   Limit Price: {mid:.4f}\n"
                f"   Credit Received: ${abs(mid) * 100:.2f} per spread\n"
                f"   Total Credit: ${abs(mid) * 100 * self.config_quantity:.2f}\n"
                f"   Signal Age: {signal_age:.1f}s\n"
                f"   Stop Loss at: ${abs(mid) * 100 * self.stop_loss_multiplier:.2f}\n"
                f"   Take Profit at: ${self.take_profit_amount:.2f}\n"
                f"═══════════════════════════════════════════════════════════════"
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
        else:
            # Log why we're not entering yet
            self.logger.debug(
                f"Waiting for better price: Mid={mid:.4f}, Target<={target_price:.4f}, "
                f"Need credit >= ${self.min_credit_amount:.2f}, Current credit=${credit_received:.2f}"
            )

    def _cancel_entry(self):
        """Cancel the entry process and clean up."""
        self.logger.info(
            f"🚫 Entry process cancelled. Cleaning up...\n"
            f"   Direction was: {self._signal_direction}\n"
            f"   Found legs: {len(self._found_legs)}"
        )
        self.entry_in_progress = False
        self._signal_time = None
        self._signal_close_price = None
        self._signal_direction = None
        self._found_legs.clear()

    def _manage_open_position(self):
        """Monitor open position for stop loss and take profit."""
        if self._spread_entry_price is None:
            self.logger.debug("Position management called but no entry price recorded.")
            return

        quote = self.cache.quote_tick(self.spread_instrument.id)
        if not quote:
            self.logger.debug("Position management: No quote available for spread.")
            return

        bid = quote.bid_price.as_double()
        ask = quote.ask_price.as_double()
        mid = (bid + ask) / 2
        
        # Calculate current P&L
        entry_credit = self._spread_entry_price
        current_cost = abs(mid)  # Cost to buy back
        pnl_per_spread = (entry_credit - current_cost) * 100
        total_pnl = pnl_per_spread * self.config_quantity
        
        # STOP LOSS
        # If mid becomes more negative (spread costs more to buy back), we're losing
        stop_price = -(self._spread_entry_price * self.stop_loss_multiplier)
        if mid <= stop_price:
            self.logger.warning(
                f"═══════════════════════════════════════════════════════════════\n"
                f"🛑 STOP LOSS TRIGGERED\n"
                f"═══════════════════════════════════════════════════════════════\n"
                f"   Entry Credit: ${entry_credit * 100:.2f}\n"
                f"   Current Mid: {mid:.4f}\n"
                f"   Stop Price: {stop_price:.4f}\n"
                f"   P&L: ${total_pnl:.2f}\n"
                f"   → Closing position\n"
                f"═══════════════════════════════════════════════════════════════"
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
                f"═══════════════════════════════════════════════════════════════\n"
                f"💰 TAKE PROFIT TRIGGERED\n"
                f"═══════════════════════════════════════════════════════════════\n"
                f"   Entry Credit: ${entry_credit * 100:.2f}\n"
                f"   Current Mid: {mid:.4f}\n"
                f"   TP Price: {tp_price:.4f}\n"
                f"   P&L: ${total_pnl:.2f}\n"
                f"   → Closing position\n"
                f"═══════════════════════════════════════════════════════════════"
            )
            self.close_spread_smart()
            self._spread_entry_price = None

    def _on_entry_timeout(self, event):
        """Handle entry timeout."""
        if self.entry_in_progress:
            legs_found = len(self._found_legs)
            if self.spread_instrument:
                self.logger.info(
                    f"⏱️ Entry timeout (30s) but spread is ready. "
                    f"Continuing to wait for acceptable quote..."
                )
            else:
                self.logger.warning(
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"⏱️ ENTRY TIMEOUT - Spread Not Ready\n"
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"   Legs found: {legs_found}/2\n"
                    f"   Short strike {self._target_short_strike}: {'✓' if self._target_short_strike in self._found_legs else '✗'}\n"
                    f"   Long strike {self._target_long_strike}: {'✓' if self._target_long_strike in self._found_legs else '✗'}\n"
                    f"   → Entry cancelled\n"
                    f"═══════════════════════════════════════════════════════════════"
                )
                self._cancel_entry()

    # =========================================================================
    # STATE MANAGEMENT
    # =========================================================================

    def _reset_daily_state(self, new_date):
        """Reset all daily state for new trading day."""
        old_date = self.current_trading_day
        
        self.daily_high = None
        self.daily_low = None
        self.range_calculated = False
        self.current_trading_day = new_date
        
        self.high_breached = False
        self.low_breached = False
        self.traded_today = False
        self.entry_in_progress = False
        self._found_legs.clear()
        self._spread_entry_price = None
        self._signal_direction = None
        self._last_minute_idx = -1
        self._last_tick_price = None
        self._signal_time = None
        self._signal_close_price = None
        self._tick_count = 0
        self._range_tick_count = 0
        self._last_log_minute = -1
        
        self.logger.info(
            f"═══════════════════════════════════════════════════════════════\n"
            f"📅 NEW TRADING DAY: {new_date}\n"
            f"═══════════════════════════════════════════════════════════════\n"
            f"   Previous day: {old_date}\n"
            f"   All daily state has been RESET\n"
            f"   Range formation will begin at {self.start_time}\n"
            f"═══════════════════════════════════════════════════════════════"
        )

    def get_state(self) -> Dict[str, Any]:
        """Return strategy-specific state for persistence."""
        state = super().get_state()
        state.update({
            "daily_high": self.daily_high,
            "daily_low": self.daily_low,
            "range_calculated": self.range_calculated,
            "current_trading_day": str(self.current_trading_day) if self.current_trading_day else None,
            "high_breached": self.high_breached,
            "low_breached": self.low_breached,
            "traded_today": self.traded_today,
            "_spread_entry_price": self._spread_entry_price,
            "_target_short_strike": self._target_short_strike,
            "_target_long_strike": self._target_long_strike,
            "_signal_direction": self._signal_direction,
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
        self.low_breached = state.get("low_breached", False)
        self.traded_today = state.get("traded_today", False)
        self._spread_entry_price = state.get("_spread_entry_price")
        self._target_short_strike = state.get("_target_short_strike")
        self._target_long_strike = state.get("_target_long_strike")
        self._signal_direction = state.get("_signal_direction")
        
        self.logger.info(
            f"State restored: Range={self.daily_low}-{self.daily_high}, "
            f"Calculated={self.range_calculated}, Traded={self.traded_today}, "
            f"Direction={self._signal_direction}"
        )

    def on_stop_safe(self):
        """Clean up when strategy stops."""
        position_qty = self.get_effective_spread_quantity()
        
        self.logger.info(
            f"═══════════════════════════════════════════════════════════════\n"
            f"🛑 STRATEGY STOPPING\n"
            f"═══════════════════════════════════════════════════════════════\n"
            f"   Total ticks processed: {self._tick_count}\n"
            f"   Range ticks: {self._range_tick_count}\n"
            f"   Traded today: {self.traded_today}\n"
            f"   Open position: {position_qty}\n"
            f"═══════════════════════════════════════════════════════════════"
        )
        
        # Close any open positions
        if position_qty != 0:
            self.logger.info(f"Closing {position_qty} spread position(s) on strategy stop...")
            self.close_spread_smart()
        
        super().on_stop_safe()
        self.logger.info("SPX15MinRangeStrategy stopped.")
