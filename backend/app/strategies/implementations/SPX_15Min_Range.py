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
        self._closing_in_progress: bool = False  # Prevents duplicate close orders and log spam
        
        # Position monitoring
        self._last_position_log_time: Optional[datetime] = None
        self._position_log_interval_seconds: int = 30  # Log position status every N seconds
        self._cache_poll_interval_seconds: int = 2     # Poll cache for instruments every N seconds
        self._required_legs_count: int = 2             # Number of option legs required for spread
        
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
        self.config_quantity = int(self.strategy_config.order_size)
        self.strike_width = int(params.get("strike_width", 5))
        
        # Risk management
        self.stop_loss_multiplier = float(params.get("stop_loss_multiplier", 2.0))
        self.take_profit_amount = float(params.get("take_profit_amount", 50.0))
        
        # Strike parameters
        self.strike_step = int(params.get("strike_step", 5))
        
        
        # Signal validation
        self.signal_max_age_seconds = int(params.get("signal_max_age_seconds", 5))
        self.max_price_deviation = float(params.get("max_price_deviation", 10.0))
        self.entry_timeout_seconds = int(params.get("entry_timeout_seconds", 35))
        
        range_end_time = "Range Close" # Will be calculated/logged by base
        
        self.logger.info(
            f"🚀 SPX15MinRangeStrategy STARTING | {self.tz} | Window: {self.start_time}-{range_end_time} | Cutoff: {self.entry_cutoff_time}",
            extra={
                "extra": {
                    "event_type": "strategy_start",
                    "strategy": "SPX15MinRangeStrategy",
                    "full_config": self.strategy_config.dict(),
                    "timezone": str(self.tz),
                    "start_time": str(self.start_time),
                    "window_minutes": self.opening_range_minutes,
                    "entry_cutoff": str(self.entry_cutoff_time),
                    "min_credit": self.min_credit_amount,
                    "quantity": self.config_quantity,
                    "strike_width": self.strike_width,
                    "stop_loss_mult": self.stop_loss_multiplier,
                    "take_profit": self.take_profit_amount
                }
            }
        )

    def on_spx_ready(self):
        """Callback when SPX data stream is ready."""
        self.logger.info(
            f"✅ SPX DATA STREAM READY | Price: {self.current_spx_price:.2f}",
            extra={
                "extra": {
                    "event_type": "data_ready",
                    "symbol": "SPX",
                    "price": self.current_spx_price
                }
            }
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
        # Use DEBUG level if already traded today to reduce log spam
        log_method = self.logger.debug if self.traded_today else self.logger.info
        log_method(
            f"⏰ MINUTE CLOSE [{et_now.strftime('%H:%M')}] | Price: {close_price:.2f} | Range: [{self.or_low:.2f}-{self.or_high:.2f}]",
            extra={
                "extra": {
                    "event_type": "minute_close",
                    "timestamp": et_now.isoformat(),
                    "close_price": close_price,
                    "range_low": self.or_low,
                    "range_high": self.or_high,
                    "high_breached": self.high_breached,
                    "low_breached": self.low_breached,
                    "traded_today": self.traded_today
                }
            }
        )

        # 1. Check for breach conditions (cross-invalidation)
        if close_price > self.or_high:
            if not self.high_breached:
                self.logger.info(
                    f"🚫 HIGH BREACHED | Close: {close_price:.2f} > High: {self.or_high:.2f} | Bearish Invalidated",
                    extra={
                        "extra": {
                            "event_type": "range_breach",
                            "breach_type": "high",
                            "close_price": close_price,
                            "range_high": self.or_high,
                            "bearish_valid": False,
                            "bullish_valid": not self.low_breached
                        }
                    }
                )
                self.high_breached = True
                self.save_state()
                
        if close_price < self.or_low:
            if not self.low_breached:
                self.logger.info(
                    f"🚫 LOW BREACHED | Close: {close_price:.2f} < Low: {self.or_low:.2f} | Bullish Invalidated",
                    extra={
                        "extra": {
                            "event_type": "range_breach",
                            "breach_type": "low",
                            "close_price": close_price,
                            "range_low": self.or_low,
                            "bullish_valid": False,
                            "bearish_valid": not self.high_breached
                        }
                    }
                )
                self.low_breached = True
                self.save_state()

        # Skip if already traded or entry in progress
        if self.traded_today:
            self.logger.debug(
                f"⏭️ Already traded today | Skipping signal check",
                extra={
                    "extra": {
                        "event_type": "signal_check_skipped",
                        "reason": "already_traded"
                    }
                }
            )
            return
        if self.entry_in_progress:
            self.logger.debug(
                f"⏳ Entry already in progress | Skipping signal check",
                extra={
                    "extra": {
                        "event_type": "signal_check_skipped",
                        "reason": "entry_in_progress"
                    }
                }
            )
            return
        
        # Skip if past entry cutoff time
        current_time = et_now.time()
        if current_time >= self.entry_cutoff_time:
            self.logger.info(
                f"⏰ Past entry cutoff time | {current_time} >= {self.entry_cutoff_time} | Skipping signal check",
                extra={
                    "extra": {
                        "event_type": "signal_check_skipped",
                        "reason": "past_cutoff_time",
                        "current_time": str(current_time),
                        "cutoff_time": str(self.entry_cutoff_time)
                    }
                }
            )
            return

        # 2. Check BEARISH entry (close below Low, High not breached first)
        if close_price < self.or_low:
            if self.high_breached:
                self.logger.info(
                    f"📉 BEARISH entry BLOCKED | Close below Low ({close_price:.2f} < {self.or_low:.2f}) but High breached earlier",
                    extra={
                        "extra": {
                            "event_type": "signal_blocked",
                            "reason": "cross_invalidation",
                            "direction": "bearish",
                            "close_price": close_price,
                            "range_low": self.or_low,
                            "high_breached": True
                        }
                    }
                )
            else:
                current_price = self.current_spx_price
                price_deviation = current_price - self.or_low
                
                if price_deviation > self.max_price_deviation:
                    self.logger.info(
                        f"⚠️ BEARISH SIGNAL REJECTED - Price Bounce | Dev: {price_deviation:.2f} > Max: {self.max_price_deviation}",
                        extra={
                            "extra": {
                                "event_type": "signal_rejected",
                                "reason": "price_bounce",
                                "direction": "bearish",
                                "close_price": close_price,
                                "current_price": current_price,
                                "range_low": self.or_low,
                                "deviation": price_deviation,
                                "max_deviation": self.max_price_deviation
                            }
                        }
                    )
                    return
                
                self._signal_time = self.clock.utc_now()
                self._signal_close_price = close_price
                self._signal_direction = 'bearish'
                
                self.logger.info(
                    f"⚡ BEARISH ENTRY SIGNAL CONFIRMED | Close {close_price:.2f} < Low {self.or_low:.2f}",
                    extra={
                        "extra": {
                            "event_type": "signal_confirmed",
                            "direction": "bearish",
                            "trigger_price": close_price,
                            "range_low": self.or_low,
                            "current_price": current_price,
                            "deviation": price_deviation
                        }
                    }
                )
                self._initiate_entry_sequence()
                return

        # 3. Check BULLISH entry (close above High, Low not breached first)
        if close_price > self.or_high:
            if self.low_breached:
                self.logger.info(
                    f"📈 BULLISH entry BLOCKED | Close above High ({close_price:.2f} > {self.or_high:.2f}) but Low breached earlier",
                    extra={
                        "extra": {
                            "event_type": "signal_blocked",
                            "reason": "cross_invalidation",
                            "direction": "bullish",
                            "close_price": close_price,
                            "range_high": self.or_high,
                            "low_breached": True
                        }
                    }
                )
            else:
                current_price = self.current_spx_price
                price_deviation = self.or_high - current_price
                
                if price_deviation > self.max_price_deviation:
                    self.logger.info(
                        f"⚠️ BULLISH SIGNAL REJECTED - Price Drop | Dev: {price_deviation:.2f} > Max: {self.max_price_deviation}",
                        extra={
                            "extra": {
                                "event_type": "signal_rejected",
                                "reason": "price_drop",
                                "direction": "bullish",
                                "close_price": close_price,
                                "current_price": current_price,
                                "range_high": self.or_high,
                                "deviation": price_deviation,
                                "max_deviation": self.max_price_deviation
                            }
                        }
                    )
                    return
                
                self._signal_time = self.clock.utc_now()
                self._signal_close_price = close_price
                self._signal_direction = 'bullish'
                
                self.logger.info(
                    f"⚡ BULLISH ENTRY SIGNAL CONFIRMED | Close {close_price:.2f} > High {self.or_high:.2f}",
                    extra={
                        "extra": {
                            "event_type": "signal_confirmed",
                            "direction": "bullish",
                            "trigger_price": close_price,
                            "range_high": self.or_high,
                            "current_price": current_price,
                            "deviation": price_deviation
                        }
                    }
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
            f"🔍 INITIATING ENTRY SEQUENCE | {self._signal_direction.upper()} | {spread_type} | [{self._target_short_strike}/{self._target_long_strike}]",
            extra={
                "extra": {
                    "event_type": "entry_sequence_start",
                    "direction": self._signal_direction,
                    "spread_type": spread_type,
                    "short_leg": f"{self._target_short_strike}{option_right}",
                    "long_leg": f"{self._target_long_strike}{option_right}",
                    "width": abs(self._target_long_strike - self._target_short_strike)
                }
            }
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
            f"📡 REQUESTING OPTION CONTRACTS | Count: {len(contracts)} | Venue: CBOE",
            extra={
                "extra": {
                    "event_type": "contract_request",
                    "count": len(contracts),
                    "contracts": [f"{c['strike']}{c['right']}" for c in contracts]
                }
            }
        )
        
        try:
            self.request_instruments(
                venue=Venue("CBOE"),
                params={"ib_contracts": contracts}
            )
            self.logger.info(
                f"✅ request_instruments() called successfully | Count: {len(contracts)}",
                extra={
                    "extra": {
                        "event_type": "request_instruments_success",
                        "count": len(contracts)
                    }
                }
            )
        except Exception as e:
            self.logger.error(
                f"❌ request_instruments() FAILED | Error: {e}", 
                exc_info=True,
                extra={
                    "extra": {
                        "event_type": "request_instruments_failure",
                        "error": str(e)
                    }
                }
            )
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
            f"📋 Expected instrument IDs in cache: {self._expected_instrument_ids}",
            extra={
                "extra": {
                    "event_type": "expected_instruments",
                    "instrument_ids": self._expected_instrument_ids
                }
            }
        )
        
        # Start polling cache for instruments (since on_instrument callback doesn't work for request_instruments)
        self._cache_poll_attempt = 0
        self._max_cache_poll_attempts = 15  # 15 attempts * 2 seconds = 30 seconds total
        self.clock.set_time_alert(
            name=f"{self.id}_cache_poll",
            alert_time=self.clock.utc_now() + timedelta(seconds=self._cache_poll_interval_seconds),
            callback=self._poll_cache_for_instruments
        )
        self.logger.info(
            f"🔄 Started cache polling | Interval: {self._cache_poll_interval_seconds}s | Max Attempts: {self._max_cache_poll_attempts}",
            extra={
                "extra": {
                    "event_type": "cache_polling_start",
                    "interval_seconds": self._cache_poll_interval_seconds,
                    "max_attempts": self._max_cache_poll_attempts
                }
            }
        )
        
        # Set absolute timeout for entry process
        self.clock.set_time_alert(
            name=f"{self.id}_entry_timeout",
            alert_time=self.clock.utc_now() + timedelta(seconds=self.entry_timeout_seconds),
            callback=self._on_entry_timeout
        )
        self.logger.info(
            f"⏱️ Entry timeout set | Duration: {self.entry_timeout_seconds}s",
            extra={
                "extra": {
                    "event_type": "entry_timeout_set",
                    "duration_seconds": self.entry_timeout_seconds
                }
            }
        )

    def on_instrument(self, instrument: Instrument):
        """Handle received instruments - track option legs for both directions."""
        super().on_instrument(instrument)
        
        # Diagnostic: Log ALL instruments received (not just options)
        self.logger.info(
            f"📥 on_instrument received | ID: {instrument.id} | Type: {type(instrument).__name__}",
            extra={
                "extra": {
                    "event_type": "on_instrument_received",
                    "instrument_id": str(instrument.id),
                    "type": type(instrument).__name__,
                    "has_strike": hasattr(instrument, 'strike_price'),
                    "has_kind": hasattr(instrument, 'option_kind')
                }
            }
        )
        
        if not self.entry_in_progress:
            self.logger.info(
                f"Ignored instrument check | Entry not in progress",
                extra={
                    "extra": {
                        "event_type": "on_instrument_ignored",
                        "reason": "entry_not_in_progress"
                    }
                }
            )
            return

        # Determine expected option kind based on direction
        expected_kind = OptionKind.CALL if self._signal_direction == 'bearish' else OptionKind.PUT
        expected_kind_str = "CALL" if expected_kind == OptionKind.CALL else "PUT"
        
        # Diagnostic: Log option evaluation
        self.logger.info(
            f"📋 Evaluating instrument for entry | {instrument.id}",
            extra={
                "extra": {
                    "event_type": "evaluating_instrument",
                    "instrument_id": str(instrument.id),
                    "signal_direction": self._signal_direction,
                    "expected_kind": expected_kind_str,
                    "target_short_strike": self._target_short_strike,
                    "target_long_strike": self._target_long_strike,
                    "found_legs_count": len(self._found_legs)
                }
            }
        )

        # Check if this is an option we're looking for
        if hasattr(instrument, 'strike_price') and hasattr(instrument, 'option_kind'):
            strike = float(instrument.strike_price.as_double())
            actual_kind = instrument.option_kind
            actual_kind_str = "CALL" if actual_kind == OptionKind.CALL else "PUT"
            
            self.logger.info(
                f"🔍 Option details: Strike={strike}, Kind={actual_kind_str}",
                extra={
                    "extra": {
                        "event_type": "option_details",
                        "strike": strike,
                        "kind": actual_kind_str
                    }
                }
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
                
                self.logger.info(
                    f"❌ Not a match | Reason: {', '.join(reasons)}",
                    extra={
                        "extra": {
                            "event_type": "option_match_failed",
                            "instrument_id": str(instrument.id),
                            "reasons": reasons
                        }
                    }
                )
                
            if is_target:
                kind_str = "C" if expected_kind == OptionKind.CALL else "P"
                self.logger.info(
                    f"✅ MATCHED as {match_reason} | {instrument.id} (Strike {strike}{kind_str})",
                    extra={
                        "extra": {
                            "event_type": "option_matched",
                            "match_type": match_reason,
                            "instrument_id": str(instrument.id),
                            "strike": strike,
                            "kind": kind_str
                        }
                    }
                )
                self._found_legs[strike] = instrument
                
                # Check if we have both legs
                if len(self._found_legs) >= self._required_legs_count:
                    # Prevent duplicate spread creation requests
                    if not self._waiting_for_spread and self.spread_id is None:
                        self.logger.info(
                            f"🎯 Both legs found | Creating spread instrument...",
                            extra={
                                "extra": {
                                    "event_type": "legs_found_complete",
                                    "found_legs_count": len(self._found_legs)
                                }
                            }
                        )
                        self._create_spread_instrument()
                else:
                    self.logger.info(
                        f"⏳ Waiting for more legs | Found: {len(self._found_legs)}/{self._required_legs_count}",
                        extra={
                            "extra": {
                                "event_type": "legs_found_partial",
                                "found_legs_count": len(self._found_legs),
                                "required_legs": self._required_legs_count
                            }
                        }
                    )
        else:
            self.logger.info(
                f"Ignored non-option instrument | {instrument.id}",
                extra={
                    "extra": {
                        "event_type": "on_instrument_ignored",
                        "reason": "not_option",
                        "instrument_id": str(instrument.id)
                    }
                }
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
            f"📦 Creating spread instrument | Long: {long_inst.id} (BUY) | Short: {short_inst.id} (SELL)",
            extra={
                "extra": {
                    "event_type": "create_spread_instrument",
                    "long_leg": str(long_inst.id),
                    "short_leg": str(short_inst.id)
                }
            }
        )
        self.create_and_request_spread(legs)

    def on_spread_ready(self, instrument: Instrument):
        """Called when spread instrument is available."""
        self.logger.info(
            f"✅ SPREAD INSTRUMENT READY | ID: {instrument.id} | Waiting for quote",
            extra={
                "extra": {
                    "event_type": "spread_instrument_ready",
                    "instrument_id": str(instrument.id)
                }
            }
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
        
        self.logger.info(
            f"Spread quote | Bid: {bid:.4f} | Ask: {ask:.4f} | Mid: {mid:.4f} | Credit: ${credit_received:.2f}",
            extra={
                "extra": {
                    "event_type": "spread_quote_tick",
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "spread_width": spread_width,
                    "credit_received": credit_received
                }
            }
        )
        
        # Validate signal freshness
        if self._signal_time:
            signal_age = (self.clock.utc_now() - self._signal_time).total_seconds()
            
            if signal_age > self.signal_max_age_seconds:
                self.logger.info(
                    f"⚠️ ENTRY CANCELLED - Signal Expired | Age: {signal_age:.1f}s > {self.signal_max_age_seconds}s",
                    extra={
                        "extra": {
                            "event_type": "entry_cancelled",
                            "reason": "signal_expired",
                            "signal_age": signal_age,
                            "max_age": self.signal_max_age_seconds
                        }
                    }
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
                self.logger.info(
                    f"⚠️ ENTRY CANCELLED - SPX Price Bounce | Dev: {price_deviation:.2f} > {self.max_price_deviation}",
                    extra={
                        "extra": {
                            "event_type": "entry_cancelled",
                            "reason": "price_bounce_during_entry",
                            "current_price": self.current_spx_price,
                            "deviation": price_deviation,
                            "max_deviation": self.max_price_deviation
                        }
                    }
                )
                self._cancel_entry()
                return
        
        # Check if we can get enough credit
        # mid should be negative for credit spread, and more negative = more credit
        if mid <= target_price:
            signal_age = (self.clock.utc_now() - self._signal_time).total_seconds() if self._signal_time else 0
            
            # Round price before submission and logging
            rounded_mid = self.round_to_tick(mid, self.spread_instrument)
            
            self.logger.info(
                f"✅ ENTRY ORDER SUBMITTED | {self._signal_direction.upper()} | Qty: {self.config_quantity} | Limit: {rounded_mid:.4f} | Credit: ${abs(rounded_mid) * 100:.2f}",
                extra={
                    "extra": {
                        "event_type": "entry_submitted",
                        "direction": self._signal_direction,
                        "quantity": self.config_quantity,
                        "limit_price": rounded_mid,
                        "credit_per_spread": abs(rounded_mid) * 100,
                        "total_credit": abs(rounded_mid) * 100 * self.config_quantity,
                        "stop_loss": abs(rounded_mid) * 100 * self.stop_loss_multiplier,
                        "take_profit": self.take_profit_amount
                    }
                }
            )
            
            self.open_spread_position(
                quantity=self.config_quantity,
                is_buy=True,
                limit_price=rounded_mid
            )
            
            self.traded_today = True
            self.entry_in_progress = False
            self._spread_entry_price = abs(rounded_mid)  # Store as positive credit amount
            self._signal_time = None
            self._signal_close_price = None
            self.save_state()
        else:
            # Log why we're not entering yet

            self.logger.info(
                f"Waiting for better price | Mid: {mid:.4f} > Target: {target_price:.4f} | Credit: ${credit_received:.2f} < Min: ${self.min_credit_amount:.2f}",
                extra={
                    "extra": {
                        "event_type": "waiting_for_price",
                        "current_mid": mid,
                        "target_price": target_price,
                        "credit_received": credit_received,
                        "min_credit_required": self.min_credit_amount
                    }
                }
            )

    def _cancel_entry(self):
        """Cancel the entry process and clean up."""
        self.logger.info(
            f"🚫 Entry process cancelled | Direction: {self._signal_direction} | Found legs: {len(self._found_legs)}",
            extra={
                "extra": {
                    "event_type": "entry_process_cancelled",
                    "direction": self._signal_direction,
                    "found_legs_count": len(self._found_legs)
                }
            }
        )
        self.entry_in_progress = False
        self._signal_time = None
        self._signal_close_price = None
        self._signal_direction = None
        self._found_legs.clear()

    def _manage_open_position(self):
        """Monitor open position for stop loss and take profit."""
        # Skip if close order already submitted (waiting for fill)
        if self._closing_in_progress:
            return
        
        if self._spread_entry_price is None:
            self.logger.info(
                "Position management skipped | No entry price recorded",
                extra={
                    "extra": {
                        "event_type": "position_management_skipped",
                        "reason": "no_entry_price"
                    }
                }
            )
            return

        quote = self.cache.quote_tick(self.spread_instrument.id)
        if not quote:
            self.logger.info(
                "Position management skipped | No quote available",
                extra={
                    "extra": {
                        "event_type": "position_management_skipped",
                        "reason": "no_quote"
                    }
                }
            )
            return

        bid = quote.bid_price.as_double()
        ask = quote.ask_price.as_double()
        mid = (bid + ask) / 2
        
        # Calculate current P&L
        # CRITICAL FIX: Use actual held quantity, not config (target) quantity
        # This handles partial fills correctly so we don't overestimate P&L
        current_qty = abs(self.get_effective_spread_quantity())
        if current_qty == 0:
            # Should not happen if we are here (check in caller), but valid safety
            return

        entry_credit = self._spread_entry_price
        current_cost = abs(mid)  # Cost to buy back
        pnl_per_spread = (entry_credit - current_cost) * 100
        total_pnl = pnl_per_spread * current_qty
        
        # Calculate SL/TP prices for logging
        stop_price = -(self._spread_entry_price * self.stop_loss_multiplier)
        tp_points = self.take_profit_amount / 100.0
        required_debit = self._spread_entry_price - tp_points
        if required_debit < 0.05:
            required_debit = 0.05
        tp_price = -required_debit
        
        # Periodic position status logging (every 30 seconds)
        now = self.clock.utc_now()
        should_log = (
            self._last_position_log_time is None or
            (now - self._last_position_log_time).total_seconds() >= self._position_log_interval_seconds
        )
        
        if should_log:
            self._last_position_log_time = now
            et_now = now.astimezone(self.tz)
            
            # Calculate distances to SL and TP
            distance_to_sl = mid - stop_price  # Negative = closer to SL
            distance_to_tp = tp_price - mid    # Negative = closer to TP
            
            # Determine position health indicator
            if total_pnl > 0:
                health = "🟢 PROFIT"
            elif total_pnl > -50:
                health = "🟡 SLIGHT LOSS"
            else:
                health = "🔴 LOSS"
            
            self.logger.info(
                f"📊 POSITION STATUS | {health} | Qty: {current_qty:.1f} | P&L: ${total_pnl:+.2f} | Mid: {mid:.4f} | SL: {stop_price:.4f} | TP: {tp_price:.4f}",
                extra={
                    "extra": {
                        "event_type": "position_status",
                        "health": health,
                        "quantity": current_qty,
                        "pnl_total": total_pnl,
                        "current_mid": mid,
                        "entry_credit": entry_credit,
                        "stop_price": stop_price,
                        "tp_price": tp_price,
                        "distance_sl": distance_to_sl,
                        "distance_tp": distance_to_tp
                    }
                }
            )
        
        # STOP LOSS
        # If mid becomes more negative (spread costs more to buy back), we're losing
        # Check STOP LOSS (stop_price already calculated above)
        if mid <= stop_price:
            self.logger.info(
                f"🛑 STOP LOSS TRIGGERED | Mid: {mid:.4f} <= Stop: {stop_price:.4f} | P&L: ${total_pnl:.2f}",
                extra={
                    "extra": {
                        "event_type": "stop_loss_trigger",
                        "current_mid": mid,
                        "stop_price": stop_price,
                        "pnl": total_pnl,
                        "entry_credit": entry_credit,
                        "quantity": current_qty
                    }
                }
            )
            self._closing_in_progress = True
            
            # CRITICAL SAFETY: Cancel any lingering entry orders (e.g. partial fills)
            # preventing them from filling AFTER we decided to close.
            self.cancel_all_orders(self.spread_instrument.id)
            
            self.close_spread_smart()
            # Note: _spread_entry_price is reset in on_order_filled_safe when close is confirmed
            return

        # Check TAKE PROFIT (tp_price already calculated above)
        
        if mid >= tp_price:
            self.logger.info(
                f"💰 TAKE PROFIT TRIGGERED | Mid: {mid:.4f} >= TP: {tp_price:.4f} | P&L: ${total_pnl:.2f}",
                extra={
                    "extra": {
                        "event_type": "take_profit_trigger",
                        "current_mid": mid,
                        "tp_price": tp_price,
                        "pnl": total_pnl,
                        "entry_credit": entry_credit,
                        "quantity": current_qty
                    }
                }
            )
            self._closing_in_progress = True
            
            # CRITICAL SAFETY: Cancel any lingering entry orders
            self.cancel_all_orders(self.spread_instrument.id)

            self.close_spread_smart()
            # Note: _spread_entry_price is reset in on_order_filled_safe when close is confirmed

    def _poll_cache_for_instruments(self, event):
        """
        Poll cache for requested instruments.
        
        NautilusTrader's request_instruments() adds instruments to cache but
        does NOT trigger on_instrument callback. So we must poll the cache.
        """
        if not self.entry_in_progress:
            self.logger.info(
                "Cache poll skipped | Entry no longer in progress",
                extra={
                    "extra": {
                        "event_type": "cache_poll_skipped",
                        "reason": "entry_not_in_progress"
                    }
                }
            )
            return
        
        self._cache_poll_attempt += 1
        self.logger.info(
            f"🔍 Cache poll attempt {self._cache_poll_attempt}/{self._max_cache_poll_attempts}",
            extra={
                "extra": {
                    "event_type": "cache_poll_attempt",
                    "attempt": self._cache_poll_attempt,
                    "max_attempts": self._max_cache_poll_attempts
                }
            }
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
                            f"✅ Found in cache: {inst_id_str} (Strike {strike})",
                            extra={
                                "extra": {
                                    "event_type": "cache_hit",
                                    "instrument_id": inst_id_str,
                                    "strike": strike
                                }
                            }
                        )
                        self._found_legs[strike] = instrument
                else:
                    self.logger.info(
                        f"⏳ Not yet in cache: {inst_id_str}",
                        extra={
                            "extra": {
                                "event_type": "cache_miss",
                                "instrument_id": inst_id_str
                            }
                        }
                    )
                    
            except Exception as e:
                self.logger.info(
                    f"⚠️ Error checking cache for {inst_id_str}: {e}",
                    extra={
                        "extra": {
                            "event_type": "cache_check_error",
                            "instrument_id": inst_id_str,
                            "error": str(e)
                        }
                    }
                )
        
        if len(self._found_legs) >= self._required_legs_count:
            self.logger.info(
                f"🎯 Both legs found in cache! Short={self._target_short_strike}, Long={self._target_long_strike}",
                extra={
                    "extra": {
                        "event_type": "legs_found_cache_complete",
                        "short_strike": self._target_short_strike,
                        "long_strike": self._target_long_strike
                    }
                }
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
                alert_time=self.clock.utc_now() + timedelta(seconds=self._cache_poll_interval_seconds),
                callback=self._poll_cache_for_instruments
            )
        else:
            self.logger.info(
                f"❌ Cache polling exhausted after {self._cache_poll_attempt} attempts. "
                f"Found {len(self._found_legs)}/{self._required_legs_count} legs.",
                extra={
                    "extra": {
                        "event_type": "cache_poll_exhausted",
                        "attempts": self._cache_poll_attempt,
                        "legs_found_count": len(self._found_legs),
                        "required_legs": self._required_legs_count,
                        "found_legs_strikes": list(self._found_legs.keys())
                    }
                }
            )

    def _on_entry_timeout(self, event):
        """Handle entry timeout."""
        if self.entry_in_progress:
            legs_found = len(self._found_legs)
            if self.spread_instrument:
                self.logger.info(
                    f"⏱️ Entry timeout ({self.entry_timeout_seconds}s) but spread is ready. Continuing to wait for acceptable quote...",
                    extra={
                        "extra": {
                            "event_type": "entry_timeout_spread_ready",
                            "timeout_seconds": self.entry_timeout_seconds,
                            "action": "continue_waiting"
                        }
                    }
                )
            else:
                # Cancel cache polling
                try:
                    self.clock.cancel_timer(f"{self.id}_cache_poll")
                except Exception:
                    pass
                
                self.logger.info(
                    f"⏱️ ENTRY TIMEOUT - Spread Not Ready | Legs: {legs_found}/{self._required_legs_count} | Short {self._target_short_strike}: {'✓' if self._target_short_strike in self._found_legs else '✗'} | Long {self._target_long_strike}: {'✓' if self._target_long_strike in self._found_legs else '✗'}",
                    extra={
                        "extra": {
                            "event_type": "entry_timeout",
                            "reason": "spread_not_ready",
                            "legs_found_count": legs_found,
                            "required_legs": self._required_legs_count,
                            "short_strike_found": self._target_short_strike in self._found_legs,
                            "long_strike_found": self._target_long_strike in self._found_legs
                        }
                    }
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
        self._last_log_minute = -1
        self._closing_in_progress = False
        
        self.logger.info(
            f"📅 NEW TRADING DAY: {new_date} | Previous: {old_date} | Range Start: {self.start_time}",
            extra={
                "extra": {
                    "event_type": "new_trading_day",
                    "date": str(new_date),
                    "previous_date": str(old_date),
                    "start_time": str(self.start_time)
                }
            }
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
            "_closing_in_progress": self._closing_in_progress,
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
        self._closing_in_progress = state.get("_closing_in_progress", False)
        
        self.logger.info(
            f"State restored | Range: {self.daily_low}-{self.daily_high} | Calculated: {self.range_calculated} | Traded: {self.traded_today} | Dir: {self._signal_direction}",
            extra={
                "extra": {
                    "event_type": "state_restored",
                    "daily_low": self.daily_low,
                    "daily_high": self.daily_high,
                    "range_calculated": self.range_calculated,
                    "traded_today": self.traded_today,
                    "signal_direction": self._signal_direction
                }
            }
        )

    def on_stop_safe(self):
        """Clean up when strategy stops."""
        position_qty = self.get_effective_spread_quantity()
        
        self.logger.info(
            f"🛑 STRATEGY STOPPING | Traded: {self.traded_today} | Pos: {position_qty}",
            extra={
                "extra": {
                    "event_type": "strategy_stop",
                    "traded_today": self.traded_today,
                    "position_quantity": position_qty
                }
            }
        )
        
        # Close any open positions
        if position_qty != 0:
            self.logger.info(
                f"🛑 Closing position on stop | Quantity: {position_qty}",
                extra={
                    "extra": {
                        "event_type": "strategy_stop_close_position",
                        "quantity": position_qty
                    }
                }
            )
            self.close_spread_smart()
        
        super().on_stop_safe()
        self.logger.info(
            "🛑 SPX15MinRangeStrategy stopped",
            extra={
                "extra": {
                    "event_type": "strategy_stopped_final"
                }
            }
        )

    def on_order_filled_safe(self, event):
        """Handle order fill events - reset closing state when close order is filled."""
        # Check if this is a close order fill (we were in closing state)
        if self._closing_in_progress:
            # Verify position is now flat (close order was filled)
            effective_qty = self.get_effective_spread_quantity()
            if effective_qty == 0:
                self.logger.info(
                    "✅ Position close confirmed | Resetting spread state",
                    extra={
                        "extra": {
                            "event_type": "position_close_confirmed",
                            "previous_entry_price": self._spread_entry_price
                        }
                    }
                )
                self._spread_entry_price = None
                self._closing_in_progress = False
                self.save_state()
