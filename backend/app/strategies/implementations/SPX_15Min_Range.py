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
from nautilus_trader.model.enums import OptionKind, TimeInForce, OrderStatus
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, Venue
from nautilus_trader.model.instruments import Instrument

from app.strategies.base_spx import SPXBaseStrategy
from app.strategies.config import StrategyConfig
from app.services.trading_data_service import TradingDataService
from app.services.telegram_service import TelegramNotificationService

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
    - stop_loss_multiplier: float (default 2.0) [REMOVED]
    - fixed_stop_loss_amount: float (default 50.0)
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
        self._sl_triggered: bool = False  # Prevents SL from re-triggering after first fire
        self._entry_order_id: Optional[ClientOrderId] = None  # Track entry order for fill timeout
        
        # Telegram
        self.telegram = TelegramNotificationService()
        
        # Position monitoring
        self._last_position_log_time: Optional[datetime] = None
        self._position_log_interval_seconds: int = 30  # Log position status every N seconds
        self._cache_poll_interval_seconds: int = 2     # Poll cache for instruments every N seconds
        self._required_legs_count: int = 2             # Number of option legs required for spread

        # Trading data service (orders + trades + drawdown tracking)
        self._trading_data = TradingDataService(db_path="data/trading.db")
        self._current_trade_id: Optional[str] = None
        self._total_commission: float = 0.0  # Track total commission for the trade
        
        # Calculate range end time for logging
        range_end_time = "Range Close" # Will be calculated/logged by base
        
    # =========================================================================
    # NOTIFICATIONS
    # =========================================================================

    def _notify(self, message: str):
        """Send notification via Telegram service."""
        if hasattr(self, 'telegram'):
            # The service handles its own background threading
            self.logger.info(f"Triggering Telegram notification: {message[:50]}...")
            self.telegram.send_message(f"{message}")
        else:
            self.logger.warning("Telegram service not initialized, skipping notification")      

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
        self.fixed_stop_loss_amount = float(params.get("fixed_stop_loss_amount", 70.0))
        self.take_profit_amount = float(params.get("take_profit_amount", 50.0))
        
        # Strike parameters
        self.strike_step = int(params.get("strike_step", 5))
        
        
        # Signal validation
        self.signal_max_age_seconds = int(params.get("signal_max_age_seconds", 5))
        self.max_price_deviation = float(params.get("max_price_deviation", 10.0))
        self.entry_timeout_seconds = int(params.get("entry_timeout_seconds", 35))
        self.fill_timeout_seconds = int(params.get("fill_timeout_seconds", 0))  # 0 = disabled
        
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
                    "fixed_stop_loss": self.fixed_stop_loss_amount,
                    "take_profit": self.take_profit_amount
                }
            }
        )
        self._notify(
            f"🚀 SPX15MinRangeStrategy STARTING | {self.tz} | Window: {self.start_time}-{range_end_time} | Cutoff: {self.entry_cutoff_time}"
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
                self._notify(
                    f"📉 BEARISH entry BLOCKED | Close below Low ({close_price:.2f} < {self.or_low:.2f}) but High breached earlier"
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
                    self._notify(
                        f"⚠️ BEARISH SIGNAL REJECTED - Price Bounce | Dev: {price_deviation:.2f} > Max: {self.max_price_deviation}"
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
                self._notify(
                    f"⚡ BEARISH ENTRY SIGNAL CONFIRMED | Close {close_price:.2f} < Low {self.or_low:.2f}"
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
                self._notify(
                    f"📈 BULLISH entry BLOCKED | Close above High ({close_price:.2f} > {self.or_high:.2f}) but Low breached earlier"
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
                    self._notify(
                        f"⚠️ BULLISH SIGNAL REJECTED - Price Drop | Dev: {price_deviation:.2f} > Max: {self.max_price_deviation}"
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
                self._notify(
                    f"⚡ BULLISH ENTRY SIGNAL CONFIRMED | Close {close_price:.2f} > High {self.or_high:.2f}"
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
        
        # Schedule fallback polling to start in 7 seconds (only if on_instrument doesn't find legs)
        # Primary mechanism is on_instrument callback; polling is backup only
        self._cache_poll_attempt = 0
        self._max_cache_poll_attempts = 15  # 15 attempts * 2 seconds = 30 seconds total
        self._fallback_polling_delay_seconds = 7
        self.clock.set_time_alert(
            name=f"{self.id}_fallback_poll_start",
            alert_time=self.clock.utc_now() + timedelta(seconds=self._fallback_polling_delay_seconds),
            callback=self._start_fallback_polling
        )
        self.logger.info(
            f"⏳ Fallback polling scheduled | Delay: {self._fallback_polling_delay_seconds}s | Will start only if on_instrument fails",
            extra={
                "extra": {
                    "event_type": "fallback_polling_scheduled",
                    "delay_seconds": self._fallback_polling_delay_seconds
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
                        # Cancel fallback polling timer since we found legs via on_instrument
                        try:
                            self.clock.cancel_timer(f"{self.id}_fallback_poll_start")
                        except Exception:
                            pass
                        try:
                            self.clock.cancel_timer(f"{self.id}_cache_poll")
                        except Exception:
                            pass
                        
                        self.logger.info(
                            f"🎯 Both legs found via on_instrument | Creating spread instrument...",
                            extra={
                                "extra": {
                                    "event_type": "legs_found_complete",
                                    "found_legs_count": len(self._found_legs),
                                    "source": "on_instrument_callback"
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
                        "stop_loss": self.fixed_stop_loss_amount,
                        "take_profit": self.take_profit_amount
                    }
                }
            )
            self._notify(
                f"✅ ENTRY ORDER SUBMITTED | {self._signal_direction.upper()} | Qty: {self.config_quantity} | Limit: {rounded_mid:.4f} | Credit: ${abs(rounded_mid) * 100:.2f}"
            )
            
            result = self.open_spread_position(
                quantity=self.config_quantity,
                is_buy=True,
                limit_price=rounded_mid
            )
            
            # Track entry order ID for fill timeout
            if result:
                # Find the entry order we just submitted (most recent in _pending_spread_orders)
                # This assumes _pending_spread_orders is updated immediately after open_spread_position
                # and that it contains only the most recent order if multiple are submitted in quick succession.
                # A more robust way would be to have open_spread_position return the ClientOrderId.
                if self._pending_spread_orders:
                    self._entry_order_id = list(self._pending_spread_orders)[-1]
                
                # Start fill timeout timer if configured
                if self.fill_timeout_seconds > 0 and self._entry_order_id:
                    try:
                        self.clock.set_time_alert(
                            name=f"{self.id}_fill_timeout",
                            alert_time=self.clock.utc_now() + timedelta(seconds=self.fill_timeout_seconds),
                            callback=self._on_fill_timeout
                        )
                        self.logger.info(
                            f"⏱️ Fill timeout set | Duration: {self.fill_timeout_seconds}s | Order: {self._entry_order_id}",
                            extra={
                                "extra": {
                                    "event_type": "fill_timeout_set",
                                    "timeout_seconds": self.fill_timeout_seconds,
                                    "order_id": str(self._entry_order_id)
                                }
                            }
                        )
                    except Exception as e:
                        self.logger.warning(f"Failed to set fill timeout timer: {e}")
            
            self.traded_today = True
            self.entry_in_progress = False
            self._spread_entry_price = abs(rounded_mid)  # Store as positive credit amount
            self._signal_time = None
            self._signal_close_price = None
            
            # Start trade tracking with TradingDataService
            now = self.clock.utc_now().astimezone(self.tz)
            entry_time_iso = now.isoformat()
            
            # Generate unique trade ID
            trade_date_str = now.strftime("%Y%m%d")
            trade_time_str = now.strftime("%H%M%S")
            self._current_trade_id = f"T-SPX-{trade_date_str}-{trade_time_str}"
            
            # Safely capture strike and premium info (may be None if something went wrong)
            try:
                short_strike = self._target_short_strike
                long_strike = self._target_long_strike
                entry_premium = abs(rounded_mid) * 100  # Premium in dollars per spread
                
                # Determine trade type based on signal direction
                if self._signal_direction == 'bearish':
                    trade_type = "CALL_CREDIT_SPREAD"
                else:
                    trade_type = "PUT_CREDIT_SPREAD"
                
                # Build strikes list
                kind_char = "C" if self._signal_direction == 'bearish' else "P"
                strikes_list = [f"{int(short_strike)}{kind_char}", f"{int(long_strike)}{kind_char}"]
                
                # Build legs info
                legs_info = [
                    {"strike": short_strike, "side": "SELL", "type": kind_char},
                    {"strike": long_strike, "side": "BUY", "type": kind_char}
                ]
                
                # Calculate max profit and max loss
                max_profit = entry_premium * self.config_quantity  # Max profit = credit received
                spread_width = abs(short_strike - long_strike)
                max_loss = (spread_width * 100 - entry_premium) * self.config_quantity
                
                # Entry reason context
                entry_reason = {
                    "trigger": self._signal_direction.upper() + "_BREAKOUT",
                    "close_price": self._signal_close_price,
                    "range_high": self.or_high,
                    "range_low": self.or_low,
                    "current_spx": self.current_spx_price
                }
                
                # Strategy config snapshot
                strategy_config_snapshot = {
                    "fixed_sl_amount": self.fixed_stop_loss_amount,
                    "tp_amount": self.take_profit_amount,
                    "min_credit": self.min_credit_amount,
                    "quantity": self.config_quantity,
                    "width": self.strike_width
                }
                
                # Calculate stop loss and take profit levels
                # Calculate stop loss and take profit levels
                # Old logic: entry_stop_loss = -(abs(rounded_mid) * self.stop_loss_multiplier)
                # New logic: stop_price = -(self._spread_entry_price + (self.fixed_stop_loss_amount / 100.0))
                # Note: self._spread_entry_price is set to abs(rounded_mid) above.
                entry_stop_loss = -(abs(rounded_mid) + self.fixed_stop_loss_amount / 100.0)
                tp_points = self.take_profit_amount / 100.0
                entry_target_price = -(abs(rounded_mid) - tp_points)
                
            except Exception as e:
                self.logger.warning(f"Error capturing trade context: {e}")
                short_strike = None
                long_strike = None
                entry_premium = None
                trade_type = "CREDIT_SPREAD"
                strikes_list = None
                legs_info = None
                max_profit = None
                max_loss = None
                entry_reason = None
                strategy_config_snapshot = None
                entry_stop_loss = None
                entry_target_price = None
            
            # Create trade record
            self._trading_data.start_trade(
                trade_id=self._current_trade_id,
                strategy_id=self.strategy_id,
                instrument_id=str(self.spread_instrument.id) if self.spread_instrument else "UNKNOWN",
                trade_type=trade_type,
                entry_price=rounded_mid,  # Negative for credit
                quantity=self.config_quantity,
                direction="LONG",  # We BUY the spread (credit spread is long combo)
                entry_time=entry_time_iso,
                entry_reason=entry_reason,
                entry_target_price=entry_target_price,
                entry_stop_loss=entry_stop_loss,
                strikes=strikes_list,
                expiration=now.strftime("%Y-%m-%d"),  # 0DTE
                legs=legs_info,
                strategy_config=strategy_config_snapshot,
                max_profit=max_profit,
                max_loss=max_loss,
                entry_premium_per_contract=entry_premium,
            )
            
            # Record entry order
            self._trading_data.record_order(
                strategy_id=self.strategy_id,
                instrument_id=str(self.spread_instrument.id) if self.spread_instrument else "UNKNOWN",
                trade_type=trade_type,
                trade_direction="ENTRY",
                order_side="BUY",
                order_type="LIMIT",
                quantity=self.config_quantity,
                status="FILLED",  # We record on fill
                submitted_time=entry_time_iso,
                trade_id=self._current_trade_id,
                client_order_id=f"{self._current_trade_id}-ENTRY",
                price_limit=rounded_mid,
                filled_time=entry_time_iso,
                filled_quantity=self.config_quantity,
                filled_price=rounded_mid,
                commission=0.0,
                raw_data=entry_reason,
            )

            
            self.save_state()
        else:
            # Log why we're not entering yet

            self.logger.info(
                f"Waiting for better price | Bid: {bid:.4f} | Ask: {ask:.4f} | Mid: {mid:.4f} > Target: {target_price:.4f} | Credit: ${credit_received:.2f} < Min: ${self.min_credit_amount:.2f}",
                extra={
                    "extra": {
                        "event_type": "waiting_for_price",
                        "current_bid": bid,
                        "current_ask": ask,
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
        # NOTE: We partially override this below for SL priority
        
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
        
        # Update trade metrics (drawdown tracking) with per-contract P&L
        if self._current_trade_id:
            self._trading_data.update_trade_metrics(
                trade_id=self._current_trade_id,
                current_pnl=pnl_per_spread
            )
        
        # Calculate SL/TP prices for logging
        # Calculate SL/TP prices for logging
        # Logic: stop_price = -(self._spread_entry_price + (self.fixed_stop_loss_amount / 100.0))
        stop_price = -(self._spread_entry_price + (self.fixed_stop_loss_amount / 100.0))
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
                f"📊 POSITION STATUS | {health} | Qty: {current_qty:.1f} | P&L: ${total_pnl:+.2f} | Mid: {mid:.4f} | Bid: {bid:.4f} | Ask: {ask:.4f} | SL: {stop_price:.4f} | TP: {tp_price:.4f}",
                extra={
                    "extra": {
                        "event_type": "position_status",
                        "health": health,
                        "quantity": current_qty,
                        "pnl_total": total_pnl,
                        "current_mid": mid,
                        "current_bid": bid,
                        "current_ask": ask,
                        "entry_credit": entry_credit,
                        "stop_price": stop_price,
                        "tp_price": tp_price,
                        "distance_sl": distance_to_sl,
                        "distance_tp": distance_to_tp
                    }
                }
            )
        
        # STOP LOSS
        # Check SL trigger BEFORE checking closing flag to allow override
        # If SL is triggered, cancel any existing orders (including active TP) and submit SL
        if mid <= stop_price and not self._sl_triggered:
            # Get any active orders to cancel
            orders_cancelled = False
            if self.spread_instrument:
                active_orders = list(self.cache.orders_open(instrument_id=self.spread_instrument.id))
                if active_orders:
                    self.logger.warning(
                        f"🛑 STOP LOSS OVERRIDE | Cancelling {len(active_orders)} pending orders to execute STOP LOSS",
                        extra={
                            "extra": {
                                "event_type": "sl_override_cancel",
                                "count": len(active_orders),
                                "orders": [str(o.client_order_id) for o in active_orders]
                            }
                        }
                    )
                    self.cancel_all_orders(self.spread_instrument.id)
                    orders_cancelled = True
            
            self.logger.info(
                f"🛑 STOP LOSS TRIGGERED | Bid: {bid:.4f} | Ask: {ask:.4f} | Mid: {mid:.4f} <= Stop: {stop_price:.4f} | P&L: ${total_pnl:.2f}",
                extra={
                    "extra": {
                        "event_type": "stop_loss_trigger",
                        "current_mid": mid,
                        "current_bid": bid,
                        "current_ask": ask,
                        "stop_price": stop_price,
                        "pnl": total_pnl,
                        "entry_credit": entry_credit,
                        "quantity": current_qty,
                        "override_active": orders_cancelled
                    }
                }
            )
            self._notify(
                f"🛑 STOP LOSS TRIGGERED | Bid: {bid:.4f} | Ask: {ask:.4f} | Mid: {mid:.4f} <= Stop: {stop_price:.4f} | P&L: ${total_pnl:.2f}"
            )
            self._closing_in_progress = True
            self._sl_triggered = True
            
            # Use aggressive price (Limit below mid) or Market for SL
            # Here we use Limit at mid - 0.05 for immediate fill
            sl_limit = mid - 0.05
            self.close_spread_smart(limit_price=sl_limit)
            return

        # Now check closing flag (TP order might be pending)
        # If we are already closing (and SL didn't trigger above), we wait
        if self._closing_in_progress:
            return

        # Check TAKE PROFIT (tp_price already calculated above)
        if mid >= tp_price:
            self.logger.info(
                f"💰 TAKE PROFIT TRIGGERED | Bid: {bid:.4f} | Ask: {ask:.4f} | Mid: {mid:.4f} >= TP: {tp_price:.4f} | P&L: ${total_pnl:.2f}",
                extra={
                    "extra": {
                        "event_type": "take_profit_trigger",
                        "current_mid": mid,
                        "current_bid": bid,
                        "current_ask": ask,
                        "tp_price": tp_price,
                        "pnl": total_pnl,
                        "entry_credit": entry_credit,
                        "quantity": current_qty
                    }
                }
            )
            self._notify(
                f"💰 TAKE PROFIT TRIGGERED | Bid: {bid:.4f} | Ask: {ask:.4f} | Mid: {mid:.4f} >= TP: {tp_price:.4f} | P&L: ${total_pnl:.2f}"
            )
            self._closing_in_progress = True
            
            # CRITICAL SAFETY: Cancel any lingering entry orders
            self.cancel_all_orders(self.spread_instrument.id)

            self.close_spread_smart(limit_price=tp_price)
            # Note: _spread_entry_price is reset in on_order_filled_safe when close is confirmed

    def _start_fallback_polling(self, event):
        """
        Start cache polling only if on_instrument callback hasn't found all legs yet.
        
        This is called after a 7-second delay to give on_instrument priority.
        If legs are already found, polling is skipped entirely.
        """
        if not self.entry_in_progress:
            self.logger.info(
                "Fallback polling skipped | Entry no longer in progress",
                extra={
                    "extra": {
                        "event_type": "fallback_polling_skipped",
                        "reason": "entry_not_in_progress"
                    }
                }
            )
            return
        
        # Check if on_instrument already found all legs
        if len(self._found_legs) >= self._required_legs_count:
            self.logger.info(
                f"Fallback polling skipped | Legs already found via on_instrument",
                extra={
                    "extra": {
                        "event_type": "fallback_polling_skipped",
                        "reason": "legs_already_found",
                        "found_legs_count": len(self._found_legs)
                    }
                }
            )
            return
        
        # Check if spread is already being created
        if self._waiting_for_spread or self.spread_id is not None:
            self.logger.info(
                f"Fallback polling skipped | Spread already in progress",
                extra={
                    "extra": {
                        "event_type": "fallback_polling_skipped",
                        "reason": "spread_in_progress",
                        "waiting_for_spread": self._waiting_for_spread,
                        "spread_id": str(self.spread_id) if self.spread_id else None
                    }
                }
            )
            return
        
        # on_instrument didn't find all legs in 7 seconds - start fallback polling
        self.logger.warning(
            f"⚠️ Starting fallback polling | on_instrument found {len(self._found_legs)}/{self._required_legs_count} legs after {self._fallback_polling_delay_seconds}s",
            extra={
                "extra": {
                    "event_type": "fallback_polling_started",
                    "legs_found_count": len(self._found_legs),
                    "required_legs": self._required_legs_count,
                    "delay_elapsed": self._fallback_polling_delay_seconds
                }
            }
        )
        
        # Start the first poll immediately
        self._poll_cache_for_instruments(event)

    def _poll_cache_for_instruments(self, event):
        """
        Poll cache for requested instruments.
        
        This is a FALLBACK mechanism that runs only if on_instrument callback
        doesn't find all legs within 7 seconds.
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
            
            # Guard against duplicate spread creation (same check as in on_instrument)
            if self._waiting_for_spread or self.spread_id is not None:
                self.logger.info(
                    f"⏭️ Spread already being created | Skipping duplicate creation from cache poll",
                    extra={
                        "extra": {
                            "event_type": "spread_creation_skipped",
                            "reason": "already_in_progress",
                            "waiting_for_spread": self._waiting_for_spread,
                            "spread_id": str(self.spread_id) if self.spread_id else None
                        }
                    }
                )
                return
            
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
                # Cancel cache polling and fallback timers
                try:
                    self.clock.cancel_timer(f"{self.id}_fallback_poll_start")
                except Exception:
                    pass
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

    def _on_fill_timeout(self, event):
        """
        Handle fill timeout - cancel unfilled portion of entry order.
        
        If order has partial fills: cancel remaining, continue with partial position.
        If no fills at all: cancel entire order, clean up entry state.
        """
        if not self._entry_order_id:
            self.logger.info(
                "Fill timeout fired but no entry order tracked | Ignoring",
                extra={
                    "extra": {
                        "event_type": "fill_timeout_no_order"
                    }
                }
            )
            return
        
        order = self.cache.order(self._entry_order_id)
        if not order:
            self.logger.warning(
                f"Fill timeout fired but order not found in cache | ID: {self._entry_order_id}",
                extra={
                    "extra": {
                        "event_type": "fill_timeout_order_not_found",
                        "order_id": str(self._entry_order_id)
                    }
                }
            )
            return
        
        # Only act if order is still pending or partially filled
        if order.status not in [OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED]:
            self.logger.info(
                f"Fill timeout fired but order already {order.status.name} | ID: {self._entry_order_id}",
                extra={
                    "extra": {
                        "event_type": "fill_timeout_order_completed",
                        "order_id": str(self._entry_order_id),
                        "order_status": order.status.name
                    }
                }
            )
            return
        
        filled_qty = float(order.filled_qty)
        total_qty = float(order.quantity)
        unfilled_qty = total_qty - filled_qty
        
        if filled_qty > 0:
            # Partial fill scenario: cancel unfilled portion, keep partial position
            self.logger.warning(
                f"⏱️ FILL TIMEOUT | Partial Fill | Filled: {filled_qty:.0f}/{total_qty:.0f} | "
                f"Cancelling unfilled: {unfilled_qty:.0f} contracts | Order: {self._entry_order_id}",
                extra={
                    "extra": {
                        "event_type": "fill_timeout_partial",
                        "order_id": str(self._entry_order_id),
                        "filled_qty": filled_qty,
                        "total_qty": total_qty,
                        "unfilled_qty": unfilled_qty,
                        "timeout_seconds": self.fill_timeout_seconds
                    }
                }
            )
            self._notify(
                f"⏱️ FILL TIMEOUT | Filled: {filled_qty:.0f}/{total_qty:.0f} | "
                f"Cancelling unfilled: {unfilled_qty:.0f} contracts"
            )
            self.cancel_order(order)
            # Position already exists with filled_qty contracts
            # SL/TP will work correctly via get_effective_spread_quantity()
        else:
            # No fills at all: cancel entire order and clean up
            self.logger.warning(
                f"⏱️ FILL TIMEOUT | No fills received in {self.fill_timeout_seconds}s | "
                f"Cancelling order: {self._entry_order_id}",
                extra={
                    "extra": {
                        "event_type": "fill_timeout_no_fills",
                        "order_id": str(self._entry_order_id),
                        "total_qty": total_qty,
                        "timeout_seconds": self.fill_timeout_seconds
                    }
                }
            )
            self._notify(
                f"⏱️ FILL TIMEOUT | No fills in {self.fill_timeout_seconds}s | Order cancelled"
            )
            self.cancel_order(order)
            # Clean up entry state since we never entered
            self._spread_entry_price = None
            self._entry_order_id = None

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
        self._sl_triggered = False
        self._entry_order_id = None
        
        # Cancel fill timeout timer if active
        try:
            self.clock.cancel_timer(f"{self.id}_fill_timeout")
        except Exception:
            pass
        
        # Cancel any orphaned trade tracking from previous day
        if self._current_trade_id:
            self._trading_data.cancel_trade(self._current_trade_id)
            self._current_trade_id = None
        
        self._total_commission = 0.0
        self._processed_executions = set()

        
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
        self._notify(
            f"📅 NEW TRADING DAY: {new_date} | Previous: {old_date} | Range Start: {self.start_time}"
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
            "_sl_triggered": self._sl_triggered,
            "_current_trade_id": self._current_trade_id,
            "_total_commission": self._total_commission,
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
        self._sl_triggered = state.get("_sl_triggered", False)
        self._current_trade_id = state.get("_current_trade_id")
        self._total_commission = state.get("_total_commission", 0.0)
        
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
        
        # Cancel fill timeout if entry order is fully filled
        if self._entry_order_id and event.client_order_id == self._entry_order_id:
            order = self.cache.order(self._entry_order_id)
            if order and order.status == OrderStatus.FILLED:
                try:
                    self.clock.cancel_timer(f"{self.id}_fill_timeout")
                except Exception:
                    pass
                self.logger.info(
                    f"⏱️ Fill timeout cancelled | Entry order fully filled | ID: {self._entry_order_id}",
                    extra={
                        "extra": {
                            "event_type": "fill_timeout_cancelled",
                            "order_id": str(self._entry_order_id),
                            "reason": "order_fully_filled"
                        }
                    }
                )
                self._entry_order_id = None
        
        # Track commission from any fill (Entry or Exit) with deduplication
        if event.commission:
            try:
                # Deduplication logic:
                # 1. Use trade_id (execution ID) to identify unique fills
                # 2. Prefer OptionSpread fills over leg fills if available (to capture full commission in one go)
                
                exec_id = getattr(event, "trade_id", None)
                is_duplicate = False
                
                if exec_id:
                    if exec_id in self._processed_executions:
                        self.logger.info(f"🔁 Duplicate execution commission ignored: {exec_id}")
                        is_duplicate = True
                    else:
                        self._processed_executions.add(exec_id)
                
                if not is_duplicate:
                    # Check instrument type - if we are trading spreads, we generally want to capture 
                    # the commission from the spread fill event, not individual legs, unless the broker 
                    # reports commissions ONLY on legs.
                    # Interactive Brokers can report both.
                    
                    try:
                        instrument = self.cache.instrument(event.instrument_id)
                        is_spread = hasattr(instrument, "legs") or type(instrument).__name__ == "OptionSpread"
                    except:
                        is_spread = False

                    # If it's a spread execution, or if we haven't seen this execution ID before (and it might be a leg fill where spread fill is missing), we take it.
                    # BUT, to be safe against double counting (Spread + Legs), we can choose to ONLY count commissions attached to the SPREAD instrument itself.
                    
                    if is_spread:
                        comm = event.commission.as_double()
                        self._total_commission += comm
                        self.logger.info(f"💵 Commission captured (Spread): ${comm:.2f} | Total: ${self._total_commission:.2f}")
                    else:
                        # It's a leg fill. Log it but don't add to total if we expect spread fills.
                        # However, if the broker only reports on legs, we might miss it.
                        # Given the logs showed $7.75 (Spread) vs $3.87 (Leg), the Spread one is correct/full.
                        # So we safely IGNORE leg commissions to avoid duplication.
                        self.logger.info(f"💵 Commission ignored for Leg fill: ${event.commission.as_double():.2f}")
                        
            except Exception as e:
                self.logger.warning(f"Failed to capture commission: {e}")

            # Check if this is a close order fill (we were in closing state)
        if self._closing_in_progress:
            # Check if position is flat — all legs closed
            effective_qty = self.get_effective_spread_quantity()
            
            if effective_qty == 0:
                # 1. Determine Fill Price
                # Priority: Order Avg Price > Tracked Limit Price > Event Last Price
                # CRITICAL FIX: Handle LEG orders by finding parent spread order
                fill_price = 0.0
                
                # Extract parent order ID if this is a LEG order
                # IB decomposes spread orders into LEG orders like:
                #   O-20260217-161349-001-000-261-LEG-SPXW260217C06855000
                # Parent spread order ID is:
                #   O-20260217-161349-001-000-261
                # We need the parent to get the correct spread price (avg_px or tracked limit),
                # because event.last_px for a LEG order contains the individual leg price (e.g. 11.55),
                # not the spread price (e.g. -2.35).
                order_id_to_check = event.client_order_id
                if "-LEG-" in str(event.client_order_id):
                    parent_order_id_str = str(event.client_order_id).split("-LEG-")[0]
                    parent_order_id = ClientOrderId(parent_order_id_str)
                    self.logger.info(f"🔍 LEG order detected | LEG: {event.client_order_id} | Parent: {parent_order_id}")
                    order_id_to_check = parent_order_id
                
                # Get the order to find the average fill price (handles partial fills correctly)
                order = self.cache.order(order_id_to_check)
                if order and hasattr(order, "avg_px") and order.avg_px is not None:
                    # avg_px is the weighted average price of all fills for this order
                    fill_price = order.avg_px.as_double() if hasattr(order.avg_px, "as_double") else float(order.avg_px)
                    self.logger.info(f"✅ Using order avg_px for spread exit: {fill_price} (from order {order_id_to_check})")
                else:
                    # Fallback to tracked limit price for the parent spread order
                    tracked_limit = self._active_spread_order_limits.get(order_id_to_check)
                    
                    if tracked_limit is not None:
                        fill_price = tracked_limit
                        self.logger.info(f"✅ Using tracked LIMIT price for spread exit: {fill_price} (from order {order_id_to_check})")
                    else:
                        # Last resort: use event last_px (but this should be avoided for spreads)
                        self.logger.warning(f"⚠️ No avg_px or tracked limit for {order_id_to_check}, using event last_px (may be incorrect for spreads!)")
                        fill_price = event.last_px.as_double() if hasattr(event.last_px, "as_double") else float(event.last_px)

                entry_credit = self._spread_entry_price if self._spread_entry_price is not None else 0.0
                # Note: spread prices are credits (negative) or debits (negative/positive depending on view).
                # Our entry is stored as absolute value in _spread_entry_price usually? 
                # No, database has -0.5. Strategy state usually keeps absolute credit? 
                # Let's check _check_and_submit_entry: "credit_received = abs(mid)..." 
                # But _spread_entry_price seems to be used as credit amount elsewhere.
                
                # RE-VERIFY P&L LOGIC
                # Entry Credit: 0.50 (captured as abs value usually, but let's check init)
                # Exit Cost (Debit): fill_price (e.g. -0.10, which means we PAY 0.10)
                # But wait, limit price -0.10 means we pay.
                # If fill_price is -0.10.
                # P&L = Credit - Debit = 0.50 - 0.10 = 0.40.
                
                # If _spread_entry_price is stored as positive credit (0.50):
                # And fill_price is -0.10 (Tracked limit).
                # We need abs(fill_price) = 0.10 is the Cost.
                
                current_cost = abs(fill_price)
                final_pnl = (entry_credit - current_cost) * 100  # P&L per spread (already closed)
                
                # Close trade and record exit order
                now = self.clock.utc_now().astimezone(self.tz)
                exit_time_iso = now.isoformat()
                
                # Determine exit reason based on what triggered the close
                stop_price = -(entry_credit + self.fixed_stop_loss_amount / 100.0)
                tp_points = self.take_profit_amount / 100.0
                required_debit = entry_credit - tp_points
                if required_debit < 0.05:
                    required_debit = 0.05
                tp_price = -required_debit
                
                if fill_price <= stop_price:
                    exit_reason = "STOP_LOSS"
                elif fill_price >= tp_price:
                    exit_reason = "TAKE_PROFIT"
                else:
                    exit_reason = "MANUAL"
                
                # Close trade record
                if self._current_trade_id:
                    self._trading_data.close_trade(
                        trade_id=self._current_trade_id,
                        exit_price=fill_price,
                        exit_reason=exit_reason,
                        exit_time=exit_time_iso,
                        commission=self._total_commission,
                    )
                    
                    # Record exit order
                    trade_type = "CALL_CREDIT_SPREAD" if self._signal_direction == 'bearish' else "PUT_CREDIT_SPREAD"
                    self._trading_data.record_order(
                        strategy_id=self.strategy_id,
                        instrument_id=str(self.spread_instrument.id) if self.spread_instrument else "UNKNOWN",
                        trade_type=trade_type,
                        trade_direction="EXIT",
                        order_side="SELL",
                        order_type="LIMIT",
                        quantity=self.config_quantity,
                        status="FILLED",
                        price_limit=fill_price, # We use the effective fill limit
                        submitted_time=exit_time_iso,
                        trade_id=self._current_trade_id,
                        client_order_id=f"{self._current_trade_id}-EXIT",
                        filled_time=exit_time_iso,
                        filled_quantity=self.config_quantity,
                        filled_price=fill_price,
                        commission=self._total_commission, # Include commission
                        raw_data={"trigger": exit_reason, "pnl": final_pnl},
                    )
                
                # Get max drawdown for logging

                trade_data = self._trading_data.get_trade(self._current_trade_id) if self._current_trade_id else {}
                max_dd = trade_data.get("max_unrealized_loss", 0) if trade_data else 0
                
                self.logger.info(
                    "✅ Position close confirmed | Resetting spread state",
                    extra={
                        "extra": {
                            "event_type": "position_close_confirmed",
                            "trade_id": self._current_trade_id,
                            "previous_entry_price": self._spread_entry_price,
                            "exit_price": fill_price,
                            "exit_reason": exit_reason,
                            "max_drawdown": max_dd,
                            "final_pnl": final_pnl
                        }
                    }
                )
                self._spread_entry_price = None
                self._closing_in_progress = False
                self._sl_triggered = False
                self._current_trade_id = None
                self._total_commission = 0.0  # Reset for next time (though typically once per day)
                
                # Clean up tracked order limits to prevent stale entries
                self._active_spread_order_limits.clear()
                
                self.save_state()

    # --- Close Order Failsafe Handlers ---

    def on_order_canceled_safe(self, event):
        self._handle_close_order_failure(event, "CANCELLED")

    def on_order_rejected_safe(self, event):
        self._handle_close_order_failure(event, "REJECTED")

    def on_order_expired_safe(self, event):
        self._handle_close_order_failure(event, "EXPIRED")

    def _handle_close_order_failure(self, event, reason: str):
        """Reset _closing_in_progress if a close order fails but position remains open."""
        if not self._closing_in_progress:
            return

        effective_qty = self.get_effective_spread_quantity()
        if effective_qty == 0:
            # Position is flat — the partial fills completed the close
            return

        self.logger.warning(
            f"\u26a0\ufe0f CLOSE ORDER {reason} | Position still open ({effective_qty:.0f} lots) | "
            f"Resetting _closing_in_progress to allow SL/TP re-trigger",
            extra={
                "extra": {
                    "event_type": f"close_order_{reason.lower()}_failsafe",
                    "effective_qty": effective_qty,
                    "order_id": str(event.client_order_id)
                }
            }
        )
        self._notify(
            f"\u26a0\ufe0f CLOSE ORDER {reason} | Position still open ({effective_qty:.0f} lots) | SL/TP monitoring resumed"
        )
        self._closing_in_progress = False
        self.save_state()
