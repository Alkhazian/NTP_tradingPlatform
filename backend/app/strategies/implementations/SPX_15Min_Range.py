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
    - entry_cutoff_time_str: str (default "12:00:00") - no entries after this time
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
        
        # Signal validation
        self.signal_max_age_seconds = 5
        self.max_price_deviation = 10.0
        
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
        
        # Calculate range end time for logging
        range_end_time = "Range Close" # Will be calculated/logged by base
        

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    def on_start_safe(self):
        """Initialize strategy after base class setup."""
        super().on_start_safe()
        
        # Load parameters from config
        params = self.strategy_config.parameters
        
        # Strategy-specific time settings
        start_time_str = params.get("start_time_str", "09:30:00")
        t = datetime.strptime(start_time_str, "%H:%M:%S").time()
        self.start_time = t
        # CRITICAL: Override base class market_open_time to use config value
        # Without this, the range calculation ignores start_time_str!
        self.market_open_time = t
        
        # Entry cutoff time
        entry_cutoff_str = params.get("entry_cutoff_time_str", "12:00:00")
        self.entry_cutoff_time = datetime.strptime(entry_cutoff_str, "%H:%M:%S").time()
        
        # Entry parameters
        self.min_credit_amount = float(params.get("min_credit_amount", 50.0))
        self.config_quantity = int(params.get("quantity", 2))
        self.strike_width = int(params.get("strike_width", 5))
        
        # Risk management
        self.stop_loss_multiplier = float(params.get("stop_loss_multiplier", 2.0))
        self.take_profit_amount = float(params.get("take_profit_amount", 50.0))
        
        # Strike parameters
        self.strike_step = int(params.get("strike_step", 5))
        
        # Signal validation
        self.signal_max_age_seconds = int(params.get("signal_max_age_seconds", 5))
        self.max_price_deviation = float(params.get("max_price_deviation", 10.0))
        
        range_end_time = "Range Close" # Will be calculated/logged by base
        
        self.logger.info(
            f"═══════════════════════════════════════════════════════════════\n"
            f"🚀 SPX15MinRangeStrategy STARTING\n"
            f"═══════════════════════════════════════════════════════════════\n"
            f"  🌍 Timezone: {self.tz}\n"
            f"  📅 Range Window: {self.start_time} - {range_end_time} ({self.opening_range_minutes} min)\n"
            f"  ⏰ Entry Cutoff: {self.entry_cutoff_time} (no entries after)\n"
            f"  💰 Min Credit: ${self.min_credit_amount:.2f}\n"
            f"  📏 Strike Width: {self.strike_width} pts\n"
            f"  🛡️ Strike Step: {self.strike_step} pts\n"
            f"  🛑 SL Multiplier: {self.stop_loss_multiplier}x\n"
            f"  💵 TP Amount: ${self.take_profit_amount:.2f}\n"
            f"  ⏱️ Signal Max Age: {self.signal_max_age_seconds}s\n"
            f"  📊 Max Price Deviation: {self.max_price_deviation} pts\n"
            f"  📦 Quantity: {self.config_quantity} spreads\n"
            f"  Mode: Tick-Only with Bidirectional Breakout\n"
            f"  Waiting for SPX data stream...\n"
            f"═══════════════════════════════════════════════════════════════"
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

    def on_quote_tick_safe(self, tick: QuoteTick):
        """
        Handle all quote ticks.
        
        1. SPX ticks go to parent -> on_spx_tick
        2. Spread ticks are processed for position management
        """
        super().on_quote_tick_safe(tick)
        
        # Process spread ticks for position management
        if self.spread_instrument and tick.instrument_id == self.spread_instrument.id:
            self._process_spread_tick(tick)

    def on_spx_tick(self, tick: QuoteTick):
        """Main logic executed on each SPX tick."""
        # This is now called after SPXBaseStrategy has updated prices and range
        pass

    def on_minute_closed(self, close_price: float):
        """
        Called once at the start of a new minute.
        close_price is the last tick price of the previous minute.
        Handles bidirectional breakout detection.
        """
        if not self.is_opening_range_complete():
            return
        
        et_now = self.clock.utc_now().astimezone(self.tz)
        
        # Log every minute close with full context
        self.logger.info(
            f"⏰ MINUTE CLOSE [{et_now.strftime('%H:%M')}]: Price={close_price:.2f} | "
            f"Range=[{self.or_low:.2f}-{self.or_high:.2f}] | "
            f"vs Low: {close_price - self.or_low:+.2f} | vs High: {close_price - self.or_high:+.2f} | "
            f"State: HighBreached={self.high_breached}, LowBreached={self.low_breached}, Traded={self.traded_today}"
        )

        # 1. Check for breach conditions (cross-invalidation)
        if close_price > self.or_high:
            if not self.high_breached:
                self.logger.warning(
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"🚫 HIGH BREACHED at {et_now.strftime('%H:%M:%S')}\n"
                    f"   Close: {close_price:.2f} > High: {self.or_high:.2f}\n"
                    f"   → BEARISH entry is now INVALIDATED for today\n"
                    f"   → BULLISH entry remains: {'VALID' if not self.low_breached else 'INVALID'}\n"
                    f"═══════════════════════════════════════════════════════════════"
                )
                self.high_breached = True
                self.save_state()
                
        if close_price < self.or_low:
            if not self.low_breached:
                self.logger.warning(
                    f"═══════════════════════════════════════════════════════════════\n"
                    f"🚫 LOW BREACHED at {et_now.strftime('%H:%M:%S')}\n"
                    f"   Close: {close_price:.2f} < Low: {self.or_low:.2f}\n"
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
        
        # Skip if past entry cutoff time
        current_time = et_now.time()
        if current_time >= self.entry_cutoff_time:
            self.logger.debug(
                f"Past entry cutoff time ({self.entry_cutoff_time}). "
                f"Current time: {current_time}. Skipping signal check."
            )
            return

        # 2. Check BEARISH entry (close below Low, High not breached first)
        if close_price < self.or_low:
            if self.high_breached:
                self.logger.info(
                    f"📉 Close below Low ({close_price:.2f} < {self.or_low:.2f}) "
                    f"but High was breached earlier. BEARISH entry BLOCKED."
                )
            else:
                current_price = self.current_spx_price
                price_deviation = current_price - self.or_low
                
                if price_deviation > self.max_price_deviation:
                    self.logger.warning(
                        f"═══════════════════════════════════════════════════════════════\n"
                        f"⚠️ BEARISH SIGNAL REJECTED - Price Bounce\n"
                        f"═══════════════════════════════════════════════════════════════\n"
                        f"   Close: {close_price:.2f} < Low: {self.or_low:.2f} ✓\n"
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
                    f"   Trigger: Close {close_price:.2f} < Low {self.or_low:.2f}\n"
                    f"   Current price: {current_price:.2f}\n"
                    f"   Deviation: {price_deviation:.2f} pts (max: {self.max_price_deviation})\n"
                    f"   High was NOT breached first: ✓\n"
                    f"   → Initiating CALL Credit Spread entry\n"
                    f"═══════════════════════════════════════════════════════════════"
                )
                self._initiate_entry_sequence()
                return

        # 3. Check BULLISH entry (close above High, Low not breached first)
        if close_price > self.or_high:
            if self.low_breached:
                self.logger.info(
                    f"📈 Close above High ({close_price:.2f} > {self.or_high:.2f}) "
                    f"but Low was breached earlier. BULLISH entry BLOCKED."
                )
            else:
                current_price = self.current_spx_price
                price_deviation = self.or_high - current_price
                
                if price_deviation > self.max_price_deviation:
                    self.logger.warning(
                        f"═══════════════════════════════════════════════════════════════\n"
                        f"⚠️ BULLISH SIGNAL REJECTED - Price Drop\n"
                        f"═══════════════════════════════════════════════════════════════\n"
                        f"   Close: {close_price:.2f} > High: {self.or_high:.2f} ✓\n"
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
                    f"   Trigger: Close {close_price:.2f} > High {self.or_high:.2f}\n"
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
            target_short = self.or_high + 0.01
            self._target_short_strike = math.ceil(target_short / self.strike_step) * self.strike_step
            self._target_long_strike = self._target_short_strike + self.strike_width
            option_right = "C"
            spread_type = "CALL Credit Spread"
        else:  # bullish
            # PUT CREDIT SPREAD: Short strike below Low, Long strike lower
            target_short = self.or_low - 0.01
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

        # Log full contract details for debugging
        self.logger.info(
            f"═══════════════════════════════════════════════════════════════\n"
            f"📡 REQUESTING OPTION CONTRACTS FROM IB\n"
            f"═══════════════════════════════════════════════════════════════\n"
            f"   Venue: CBOE\n"
            f"   Number of contracts: {len(contracts)}\n"
            f"   Contracts:\n" +
            "\n".join([
                f"      [{i+1}] {c['symbol']} {c['tradingClass']} {c['strike']}{c['right']} "
                f"exp={c['lastTradeDateOrContractMonth']} @ {c['exchange']}"
                for i, c in enumerate(contracts)
            ]) + "\n"
            f"═══════════════════════════════════════════════════════════════"
        )
        
        try:
            self.request_instruments(
                venue=Venue("CBOE"),
                params={"ib_contracts": contracts}
            )
            self.logger.info(f"✅ request_instruments() called successfully for {len(contracts)} contracts")
        except Exception as e:
            self.logger.error(f"❌ request_instruments() FAILED: {e}", exc_info=True)
            self._cancel_entry()
            return
        
        # Store expected instrument IDs for cache polling
        # NautilusTrader/IB format: SPXW260121P06810000.CBOE (uses 2-digit year!)
        self._expected_instrument_ids = []
        
        # Convert YYYYMMDD to YYMMDD (IB uses 2-digit year format)
        expiry_yy = today_str[2:]  # "20260121" -> "260121"
        
        for strike in [self._target_short_strike, self._target_long_strike]:
            # Format: SPXW260121P06810000.CBOE
            strike_str = f"{int(strike):05d}000"  # e.g., 6810 -> 06810000
            inst_id_str = f"SPXW{expiry_yy}{option_right}{strike_str}.CBOE"
            self._expected_instrument_ids.append(inst_id_str)
        
        self.logger.info(
            f"📋 Expected instrument IDs in cache:\n" +
            "\n".join([f"      • {inst_id}" for inst_id in self._expected_instrument_ids])
        )
        
        # Start polling cache for instruments (since on_instrument callback doesn't work for request_instruments)
        self._cache_poll_attempt = 0
        self._max_cache_poll_attempts = 15  # 15 attempts * 2 seconds = 30 seconds total
        self.clock.set_time_alert(
            name=f"{self.id}_cache_poll",
            alert_time=self.clock.utc_now() + timedelta(seconds=2),
            callback=self._poll_cache_for_instruments
        )
        self.logger.info(f"🔄 Started cache polling (every 2s, max {self._max_cache_poll_attempts} attempts)")
        
        # Set absolute timeout for entry process
        self.clock.set_time_alert(
            name=f"{self.id}_entry_timeout",
            alert_time=self.clock.utc_now() + timedelta(seconds=35),
            callback=self._on_entry_timeout
        )
        self.logger.debug(f"Entry timeout set for 35 seconds")

    def on_instrument(self, instrument: Instrument):
        """Handle received instruments - track option legs for both directions."""
        super().on_instrument(instrument)
        
        # Diagnostic: Log ALL instruments received (not just options)
        self.logger.debug(
            f"📥 on_instrument received: {instrument.id} "
            f"(type={type(instrument).__name__}, "
            f"has_strike={hasattr(instrument, 'strike_price')}, "
            f"has_kind={hasattr(instrument, 'option_kind')})"
        )
        
        if not self.entry_in_progress:
            self.logger.debug(f"   → Ignored (entry_in_progress=False)")
            return

        # Determine expected option kind based on direction
        expected_kind = OptionKind.CALL if self._signal_direction == 'bearish' else OptionKind.PUT
        expected_kind_str = "CALL" if expected_kind == OptionKind.CALL else "PUT"
        
        # Diagnostic: Log option evaluation
        self.logger.info(
            f"📋 Evaluating instrument for entry:\n"
            f"   Instrument: {instrument.id}\n"
            f"   Entry in progress: {self.entry_in_progress}\n"
            f"   Signal direction: {self._signal_direction}\n"
            f"   Expected kind: {expected_kind_str}\n"
            f"   Target short strike: {self._target_short_strike}\n"
            f"   Target long strike: {self._target_long_strike}\n"
            f"   Current found legs: {list(self._found_legs.keys())}"
        )

        # Check if this is an option we're looking for
        if hasattr(instrument, 'strike_price') and hasattr(instrument, 'option_kind'):
            strike = float(instrument.strike_price.as_double())
            actual_kind = instrument.option_kind
            actual_kind_str = "CALL" if actual_kind == OptionKind.CALL else "PUT"
            
            self.logger.info(
                f"   🔍 Option details: Strike={strike}, Kind={actual_kind_str}"
            )
            
            is_target = False
            match_reason = ""
            if strike == self._target_short_strike and actual_kind == expected_kind:
                is_target = True
                match_reason = "SHORT LEG"
            elif strike == self._target_long_strike and actual_kind == expected_kind:
                is_target = True
                match_reason = "LONG LEG"
            else:
                # Log why it didn't match
                reasons = []
                if strike != self._target_short_strike and strike != self._target_long_strike:
                    reasons.append(f"strike {strike} not in [{self._target_short_strike}, {self._target_long_strike}]")
                if actual_kind != expected_kind:
                    reasons.append(f"kind {actual_kind_str} != expected {expected_kind_str}")
                self.logger.debug(f"   ❌ Not a match: {', '.join(reasons)}")
                
            if is_target:
                kind_str = "C" if expected_kind == OptionKind.CALL else "P"
                self.logger.info(
                    f"   ✅ MATCHED as {match_reason}: {instrument.id} (Strike {strike}{kind_str})"
                )
                self._found_legs[strike] = instrument
                
                # Check if we have both legs
                if len(self._found_legs) >= 2:
                    self.logger.info(f"   🎯 Both legs found! Creating spread instrument...")
                    self._create_spread_instrument()
                else:
                    self.logger.info(f"   ⏳ Waiting for more legs ({len(self._found_legs)}/2 found)")
        else:
            self.logger.debug(
                f"   → Not an option instrument (missing strike_price or option_kind)"
            )

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
                price_deviation = self.current_spx_price - self.or_low
                level_name = "Low"
            else:  # bullish
                price_deviation = self.or_high - self.current_spx_price
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

    def _poll_cache_for_instruments(self, event):
        """
        Poll cache for requested instruments.
        
        NautilusTrader's request_instruments() adds instruments to cache but
        does NOT trigger on_instrument callback. So we must poll the cache.
        """
        if not self.entry_in_progress:
            self.logger.debug("Cache poll skipped - entry no longer in progress")
            return
        
        self._cache_poll_attempt += 1
        self.logger.info(
            f"🔍 Cache poll attempt {self._cache_poll_attempt}/{self._max_cache_poll_attempts}"
        )
        
        # Check cache for each expected instrument
        for inst_id_str in self._expected_instrument_ids:
            try:
                inst_id = InstrumentId.from_str(inst_id_str)
                instrument = self.cache.instrument(inst_id)
                
                if instrument:
                    strike = float(instrument.strike_price.as_double())
                    
                    if strike not in self._found_legs:
                        self.logger.info(
                            f"   ✅ Found in cache: {inst_id_str} (Strike {strike})"
                        )
                        self._found_legs[strike] = instrument
                else:
                    self.logger.debug(f"   ⏳ Not yet in cache: {inst_id_str}")
                    
            except Exception as e:
                self.logger.warning(f"   ⚠️ Error checking cache for {inst_id_str}: {e}")
        
        # Check if we have both legs
        if len(self._found_legs) >= 2:
            self.logger.info(
                f"🎯 Both legs found in cache! Short={self._target_short_strike}, Long={self._target_long_strike}"
            )
            # Cancel the polling timer
            try:
                self.clock.cancel_timer(f"{self.id}_cache_poll")
            except Exception:
                pass
            # Create spread
            self._create_spread_instrument()
            return
        
        # Schedule next poll if not exhausted
        if self._cache_poll_attempt < self._max_cache_poll_attempts:
            self.clock.set_time_alert(
                name=f"{self.id}_cache_poll",
                alert_time=self.clock.utc_now() + timedelta(seconds=2),
                callback=self._poll_cache_for_instruments
            )
        else:
            self.logger.warning(
                f"❌ Cache polling exhausted after {self._cache_poll_attempt} attempts. "
                f"Found {len(self._found_legs)}/2 legs."
            )

    def _on_entry_timeout(self, event):
        """Handle entry timeout."""
        if self.entry_in_progress:
            legs_found = len(self._found_legs)
            if self.spread_instrument:
                self.logger.info(
                    f"⏱️ Entry timeout (35s) but spread is ready. "
                    f"Continuing to wait for acceptable quote..."
                )
            else:
                # Cancel cache polling
                try:
                    self.clock.cancel_timer(f"{self.id}_cache_poll")
                except Exception:
                    pass
                
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
        super()._reset_daily_state(new_date)
        
        self.high_breached = False
        self.low_breached = False
        self.traded_today = False
        self.entry_in_progress = False
        self._found_legs.clear()
        self._spread_entry_price = None
        self._signal_direction = None
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
