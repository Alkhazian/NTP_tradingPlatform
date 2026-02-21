"""
SPX 1DTE Bull Put Spread Strategy

Unidirectional (bullish-only) 1DTE short put spread strategy.
Uses ES1! futures for multi-timeframe trend confirmation:
  - Daily EMA(20) for trend direction
  - Daily VWMA(14) for Bollinger basis
  - 1-Minute SMA(10) for intraday momentum

Entry: SPX breaks above 30-min Opening Range High + all ES filters pass
Exit:  Stop Loss (180% of credit) or Take Profit (40% of credit)
"""

from typing import Dict, Any, Optional
from datetime import datetime, time, timedelta, date
from collections import deque
import pytz

from nautilus_trader.model.data import QuoteTick, Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce, OptionKind
from nautilus_trader.model.identifiers import InstrumentId, Venue, ClientOrderId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.enums import OrderStatus

from app.strategies.base_spx import SPXBaseStrategy
from app.strategies.config import StrategyConfig


class SPX1DTEBullPutSpreadStrategy(SPXBaseStrategy):
    """
    1DTE Bull Put Spread strategy for bullish markets.

    Sells OTM put spreads when ES1! confirms a stable bullish trend.
    Uses delta-based option selection via custom Black-Scholes.
    """

    def __init__(self, config: StrategyConfig, integration_manager=None, persistence_manager=None):
        super().__init__(config, integration_manager, persistence_manager)

        params = config.parameters

        # --- ES1! Instrument ---
        self.es_instrument_id_str = params.get("es_instrument_id", "ESH6.CME")
        self.es_instrument_id = InstrumentId.from_str(self.es_instrument_id_str)
        self.es_instrument: Optional[Instrument] = None
        self.es_subscribed = False

        # --- Indicator Periods ---
        self.es_ema_period = int(params.get("es_ema_period", 20))
        self.es_sma_period = int(params.get("es_sma_period", 10))
        self.es_vwma_period = int(params.get("es_vwma_period", 14))

        # --- Option Selection ---
        self.short_put_delta = float(params.get("short_put_delta", -0.25))
        self.long_put_delta = float(params.get("long_put_delta", -0.14))
        self.min_credit_amount = float(params.get("min_credit_amount", 30.0))
        self.config_quantity = config.order_size

        # --- Risk Management (% of credit) ---
        self.stop_loss_pct = float(params.get("stop_loss_pct_of_credit", 180.0))
        self.take_profit_pct = float(params.get("take_profit_pct_of_credit", 40.0))

        # --- Entry Timing ---
        cutoff_str = params.get("entry_cutoff_time_str", "12:00:00")
        h, m, s = [int(x) for x in cutoff_str.split(":")]
        self.entry_cutoff_time = time(h, m, s)
        self.signal_max_age_seconds = int(params.get("signal_max_age_seconds", 180))
        self.entry_price_adjustment = float(params.get("entry_price_adjustment", 0.5))
        self.entry_timeout_seconds = int(params.get("entry_timeout_seconds", 60))
        self.fill_timeout_seconds = int(params.get("fill_timeout_seconds", 120))

        # --- Regime Filters ---
        self.require_strong_reclaim = params.get("require_strong_reclaim", True)
        self.require_two_day_confirmation = params.get("require_two_day_confirmation", False)
        self.enable_macro_filter = params.get("enable_macro_filter", True)
        self.macro_day_before = params.get("macro_day_before", True)
        macro_dates_str = params.get("macro_stop_dates", "")
        self.macro_stop_dates: set = set()
        if macro_dates_str:
            for d in macro_dates_str.split(","):
                d = d.strip()
                if d:
                    try:
                        self.macro_stop_dates.add(date.fromisoformat(d))
                    except ValueError:
                        self.logger.warning(f"Invalid macro date: {d}")

        # --- ES Indicator State (manual rolling windows) ---
        self._es_daily_closes: deque = deque(maxlen=max(self.es_ema_period, self.es_vwma_period) + 5)
        self._es_daily_volumes: deque = deque(maxlen=self.es_vwma_period + 5)
        self._es_daily_opens: deque = deque(maxlen=5)  # For green candle check
        self._es_1min_closes: deque = deque(maxlen=self.es_sma_period + 5)
        self._es_ema_value: Optional[float] = None  # Current EMA(20) value
        self._es_vwma_value: Optional[float] = None  # Current VWMA(14) value
        self._es_sma_value: Optional[float] = None   # Current SMA(10) 1-min value
        self._es_current_price: float = 0.0

        # --- Bar Types (set in on_start_safe) ---
        self._es_daily_bar_type = None
        self._es_1min_bar_type = None

        # --- Trading State ---
        self.traded_today = False
        self.entry_in_progress = False
        self._spread_entry_price: Optional[float] = None  # Credit received (positive)
        self._signal_time: Optional[datetime] = None
        self._closing_in_progress = False
        self._sl_triggered = False
        self._entry_order_id: Optional[ClientOrderId] = None
        self._current_trade_id: Optional[str] = None
        self._total_commission: float = 0.0
        self._processed_executions: set = set()
        self._last_log_minute: int = -1
        self._macro_clear_today: bool = True  # Cached daily; set in _reset_daily_state
        self._strong_reclaim_ok: bool = False  # Cached; set on ES daily bar
        self._two_day_confirmed_ok: bool = False  # Cached; set on ES daily bar

        # --- Option Search State ---
        self._short_put_search_id: Optional[str] = None
        self._long_put_search_id: Optional[str] = None
        self._found_legs: Dict[float, Instrument] = {}  # strike -> instrument
        self._target_short_strike: Optional[float] = None
        self._target_long_strike: Optional[float] = None

        # --- Services ---
        if integration_manager and hasattr(integration_manager, 'trading_data_service'):
            self._trading_data = integration_manager.trading_data_service
        else:
            self._trading_data = None

        try:
            from app.services.telegram_service import send_telegram_message
            self._telegram = send_telegram_message
        except Exception:
            self._telegram = None

        self.logger.info(
            f"SPX1DTEBullPutSpread initialized | "
            f"ES: {self.es_instrument_id_str} | "
            f"Deltas: short={self.short_put_delta}, long={self.long_put_delta} | "
            f"SL={self.stop_loss_pct}% TP={self.take_profit_pct}% of credit | "
            f"Strong Reclaim={self.require_strong_reclaim} | "
            f"2-Day Confirm={self.require_two_day_confirmation} | "
            f"Macro Filter={self.enable_macro_filter} ({len(self.macro_stop_dates)} dates)"
        )

    # =========================================================================
    # NOTIFICATIONS
    # =========================================================================

    def _notify(self, message: str):
        """Send Telegram notification (fire-and-forget)."""
        prefix = f"[{self.strategy_id}] "
        if self._telegram:
            try:
                self._telegram(prefix + message)
            except Exception:
                pass

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    def on_start_safe(self):
        """Called after primary instrument ready. Subscribe to ES data."""
        super().on_start_safe()

        # Set up ES bar types
        self._es_daily_bar_type = BarType.from_str(
            f"{self.es_instrument_id}-1-DAY-LAST-EXTERNAL"
        )
        self._es_1min_bar_type = BarType.from_str(
            f"{self.es_instrument_id}-1-MINUTE-LAST-EXTERNAL"
        )

        # Subscribe to ES bars
        self._subscribe_es_data()
        self._macro_clear_today = self._is_macro_clear()

        self.logger.info(
            f"🚀 SPX 1DTE Bull Put Spread STARTED | "
            f"OR={self.opening_range_minutes}m | "
            f"ES Daily: EMA({self.es_ema_period}), VWMA({self.es_vwma_period}) | "
            f"ES 1min: SMA({self.es_sma_period}) | "
            f"Cutoff: {self.entry_cutoff_time}"
        )
        self._notify(
            f"🚀 Strategy STARTED | OR={self.opening_range_minutes}m | "
            f"SL={self.stop_loss_pct}% TP={self.take_profit_pct}%"
        )

    def _subscribe_es_data(self):
        """Subscribe to ES futures bar data."""
        try:
            # Check if ES instrument is in cache
            self.es_instrument = self.cache.instrument(self.es_instrument_id)
            if self.es_instrument:
                self.logger.info(f"ES instrument found in cache: {self.es_instrument_id}")
            else:
                self.logger.info(f"ES instrument not in cache, requesting: {self.es_instrument_id}")
                self.request_instrument(self.es_instrument_id)

            # Subscribe to bars regardless — Nautilus handles instrument resolution
            self.subscribe_bars(self._es_daily_bar_type)
            self.subscribe_bars(self._es_1min_bar_type)
            self.es_subscribed = True

            self.logger.info(
                f"📊 Subscribed to ES bars | "
                f"Daily: {self._es_daily_bar_type} | "
                f"1min: {self._es_1min_bar_type}"
            )
        except Exception as e:
            self.logger.error(f"Failed to subscribe to ES data: {e}", exc_info=True)

    # =========================================================================
    # BAR HANDLERS — ES1! INDICATOR ENGINE
    # =========================================================================

    def on_bar(self, bar: Bar):
        """Route incoming bars to appropriate handler."""
        try:
            if bar.bar_type == self._es_daily_bar_type:
                self._handle_es_daily_bar(bar)
            elif bar.bar_type == self._es_1min_bar_type:
                self._handle_es_1min_bar(bar)
        except Exception as e:
            self.logger.error(f"Error processing bar: {e}", exc_info=True)

    def _handle_es_daily_bar(self, bar: Bar):
        """Process ES daily bar: update EMA(20) and VWMA(14)."""
        close = float(bar.close)
        open_price = float(bar.open)
        volume = float(bar.volume)

        self._es_daily_closes.append(close)
        self._es_daily_opens.append(open_price)
        self._es_daily_volumes.append(volume)

        # Update EMA(20)
        self._update_ema(close)

        # Update VWMA(14)
        self._update_vwma()

        self.logger.info(
            f"📊 ES Daily Bar | Close={close:.2f} Open={open_price:.2f} Vol={volume:.0f} | "
            f"EMA({self.es_ema_period})={self._es_ema_value:.2f if self._es_ema_value else 'N/A'} | "
            f"VWMA({self.es_vwma_period})={self._es_vwma_value:.2f if self._es_vwma_value else 'N/A'} | "
            f"Bars: {len(self._es_daily_closes)}",
            extra={"extra": {"event_type": "es_daily_bar",
                             "close": close, "ema": self._es_ema_value, "vwma": self._es_vwma_value}}
        )

        # Re-evaluate daily regime filters (only changes on new daily bars)
        self._strong_reclaim_ok = self._is_strong_reclaim()
        self._two_day_confirmed_ok = self._is_two_day_confirmed()

    def _handle_es_1min_bar(self, bar: Bar):
        """Process ES 1-minute bar: update SMA(10) and current price."""
        close = float(bar.close)
        self._es_1min_closes.append(close)
        self._es_current_price = close

        # Update SMA(10)
        if len(self._es_1min_closes) >= self.es_sma_period:
            recent = list(self._es_1min_closes)[-self.es_sma_period:]
            self._es_sma_value = sum(recent) / len(recent)

    # =========================================================================
    # INDICATOR CALCULATIONS
    # =========================================================================

    def _update_ema(self, new_close: float):
        """Update EMA(N) using standard exponential smoothing."""
        n = self.es_ema_period
        if len(self._es_daily_closes) < n:
            self._es_ema_value = None
            return

        if self._es_ema_value is None:
            # Seed EMA with SMA of first N closes
            first_n = list(self._es_daily_closes)[-n:]
            self._es_ema_value = sum(first_n) / n
        else:
            multiplier = 2.0 / (n + 1)
            self._es_ema_value = (new_close - self._es_ema_value) * multiplier + self._es_ema_value

    def _update_vwma(self):
        """
        Update VWMA(N) — Volume-Weighted Moving Average.
        VWMA = Σ(close_i × volume_i) / Σ(volume_i) for last N bars.
        """
        n = self.es_vwma_period
        if len(self._es_daily_closes) < n or len(self._es_daily_volumes) < n:
            self._es_vwma_value = None
            return

        closes = list(self._es_daily_closes)[-n:]
        volumes = list(self._es_daily_volumes)[-n:]

        total_volume = sum(volumes)
        if total_volume <= 0:
            self._es_vwma_value = None
            return

        weighted_sum = sum(c * v for c, v in zip(closes, volumes))
        self._es_vwma_value = weighted_sum / total_volume

    # =========================================================================
    # TREND FILTER CHECKS
    # =========================================================================

    def _is_es_trend_bullish(self) -> bool:
        """Check all ES1! trend conditions."""
        if self._es_ema_value is None:
            self.logger.debug("ES EMA not ready")
            return False
        if self._es_vwma_value is None:
            self.logger.debug("ES VWMA not ready")
            return False
        if self._es_sma_value is None:
            self.logger.debug("ES 1-min SMA not ready")
            return False
        if self._es_current_price <= 0:
            return False

        price = self._es_current_price
        ema_ok = price > self._es_ema_value
        vwma_ok = price > self._es_vwma_value
        sma_ok = price > self._es_sma_value

        if not (ema_ok and vwma_ok and sma_ok):
            self.logger.debug(
                f"ES trend BEARISH | Price={price:.2f} | "
                f"EMA={self._es_ema_value:.2f}({'✓' if ema_ok else '✗'}) | "
                f"VWMA={self._es_vwma_value:.2f}({'✓' if vwma_ok else '✗'}) | "
                f"SMA={self._es_sma_value:.2f}({'✓' if sma_ok else '✗'})"
            )
            return False
        return True

    def _is_strong_reclaim(self) -> bool:
        """D-1 regime check: prior day close > EMA20 AND green candle."""
        if not self.require_strong_reclaim:
            return True

        if len(self._es_daily_closes) < 2 or len(self._es_daily_opens) < 2:
            self.logger.debug("Not enough daily bars for strong reclaim check")
            return False

        # Prior day = second-to-last bar (last bar is current/forming)
        prev_close = list(self._es_daily_closes)[-2]
        prev_open = list(self._es_daily_opens)[-2]

        if self._es_ema_value is None:
            return False

        above_ema = prev_close > self._es_ema_value
        green_candle = prev_close > prev_open

        if not (above_ema and green_candle):
            self.logger.debug(
                f"Strong reclaim FAILED | PrevClose={prev_close:.2f} PrevOpen={prev_open:.2f} | "
                f"AboveEMA={'✓' if above_ema else '✗'} Green={'✓' if green_candle else '✗'}"
            )
        return above_ema and green_candle

    def _is_two_day_confirmed(self) -> bool:
        """2 consecutive daily closes above EMA20."""
        if not self.require_two_day_confirmation:
            return True

        if len(self._es_daily_closes) < 3 or self._es_ema_value is None:
            return False

        closes = list(self._es_daily_closes)
        # Check D-1 and D-2 (last two completed bars)
        return closes[-2] > self._es_ema_value and closes[-3] > self._es_ema_value

    def _is_macro_clear(self) -> bool:
        """Check if today is NOT a macro event day (or day before)."""
        if not self.enable_macro_filter or not self.macro_stop_dates:
            return True

        today = self.clock.utc_now().astimezone(self.tz).date()
        if today in self.macro_stop_dates:
            self.logger.debug(f"🚫 MACRO EVENT today ({today}) — trading blocked")
            return False

        if self.macro_day_before:
            tomorrow = today + timedelta(days=1)
            if tomorrow in self.macro_stop_dates:
                self.logger.debug(f"🚫 MACRO EVENT tomorrow ({tomorrow}) — trading blocked (day-before gate)")
                return False
        return True

    # =========================================================================
    # SPX TICK HANDLER — ENTRY LOGIC
    # =========================================================================

    def on_spx_tick(self, tick: QuoteTick):
        """Called for each SPX quote tick from base class."""
        now_et = self.clock.utc_now().astimezone(self.tz)

        # Periodic logging (every 5 min)
        current_minute = now_et.hour * 60 + now_et.minute
        if current_minute % 5 == 0 and current_minute != self._last_log_minute:
            self._last_log_minute = current_minute
            or_str = f"OR_H={self.or_high:.2f}" if self.or_high else "OR pending"
            es_str = (
                f"ES={self._es_current_price:.2f} "
                f"EMA={self._es_ema_value:.2f if self._es_ema_value else 'N/A'} "
                f"VWMA={self._es_vwma_value:.2f if self._es_vwma_value else 'N/A'} "
                f"SMA={self._es_sma_value:.2f if self._es_sma_value else 'N/A'}"
            )
            self.logger.info(
                f"📈 SPX={self.current_spx_price:.2f} | {or_str} | {es_str} | "
                f"Traded={self.traded_today} Entry={self.entry_in_progress}"
            )

        # Check for bullish breakout entry
        if (self.range_calculated and not self.traded_today
                and not self.entry_in_progress and not self._closing_in_progress
                and self.get_effective_spread_quantity() == 0):
            self._check_entry_signal()

        # Monitor open position for SL/TP
        if self._spread_entry_price is not None and not self.entry_in_progress:
            self._manage_open_position()

    def on_minute_closed(self, close_price: float):
        """Called at each SPX minute close by base class."""
        pass  # Entry logic runs on tick; no minute-close logic needed

    # =========================================================================
    # ENTRY SIGNAL DETECTION
    # =========================================================================

    def _check_entry_signal(self):
        """Check all entry conditions and initiate entry if met."""
        now_et = self.clock.utc_now().astimezone(self.tz)

        # Time gate
        if now_et.time() >= self.entry_cutoff_time:
            return
        if now_et.time() < time(9, 30):
            return

        # Opening range must be complete and price above OR high
        if not self.range_calculated or self.or_high is None:
            return
        if self.daily_high is None or self.daily_high <= self.or_high:
            return

        # ES trend filters
        if not self._is_es_trend_bullish():
            return
        if not self._strong_reclaim_ok:
            return
        if not self._two_day_confirmed_ok:
            return
        if not self._macro_clear_today:
            return

        # All conditions met — initiate entry
        self.logger.info(
            f"🔥 ALL ENTRY CONDITIONS MET | "
            f"SPX High={self.daily_high:.2f} > OR_High={self.or_high:.2f} | "
            f"ES={self._es_current_price:.2f} > EMA={self._es_ema_value:.2f} > "
            f"VWMA={self._es_vwma_value:.2f} | SMA1m={self._es_sma_value:.2f}",
            extra={"extra": {"event_type": "entry_signal",
                             "spx_high": self.daily_high, "or_high": self.or_high,
                             "es_price": self._es_current_price}}
        )
        self._notify(
            f"🔥 ENTRY SIGNAL | SPX High={self.daily_high:.2f} > OR={self.or_high:.2f} | "
            f"ES={self._es_current_price:.2f}"
        )
        self._initiate_entry()

    # =========================================================================
    # ENTRY SEQUENCE — DELTA-BASED OPTION SEARCH
    # =========================================================================

    def _initiate_entry(self):
        """Start the entry sequence: search for put options by delta."""
        self.entry_in_progress = True
        self.traded_today = True
        self._signal_time = self.clock.utc_now().astimezone(self.tz)
        self._found_legs.clear()
        
        # Set absolute timeout for entry setup/process
        self.clock.set_time_alert(
            name=f"{self.id}_entry_timeout",
            alert_time=self.clock.utc_now() + timedelta(seconds=self.entry_timeout_seconds),
            callback=self._on_entry_timeout
        )
        
        self.save_state()

        # Calculate 1DTE expiry (tomorrow)
        today = self.clock.utc_now().astimezone(self.tz).date()
        tomorrow = today + timedelta(days=1)
        # Skip weekends
        if tomorrow.weekday() == 5:  # Saturday
            tomorrow += timedelta(days=2)
        elif tomorrow.weekday() == 6:  # Sunday
            tomorrow += timedelta(days=1)
        self._1dte_expiry = tomorrow.strftime("%Y%m%d")

        self.logger.info(
            f"📋 Starting entry sequence | 1DTE Expiry: {self._1dte_expiry} | "
            f"Short Δ={self.short_put_delta} Long Δ={self.long_put_delta}"
        )

        # Search for short put (closer to ATM, higher absolute delta)
        self._short_put_search_id = self.find_option_by_delta(
            target_delta=self.short_put_delta,
            option_kind=OptionKind.PUT,
            expiry_date=self._1dte_expiry,
            strike_range=40,   # 200pt range to cover 100-150pt OTM
            strike_step=5,
            selection_delay_seconds=20.0,
            callback=self._on_short_put_found
        )

    def _on_short_put_found(self, search_id, selected_option, option_data):
        """Callback when short put delta search completes."""
        if not selected_option or not option_data:
            self._abort_entry("No short put found matching target delta")
            return

        short_strike = option_data['strike']
        self._target_short_strike = short_strike
        self._found_legs[short_strike] = selected_option

        self.logger.info(
            f"✅ SHORT PUT selected | Strike=${short_strike:.0f} | "
            f"Δ={option_data['delta']:.4f} (target={self.short_put_delta}) | "
            f"IV={option_data['iv']:.2%} Mid=${option_data['mid']:.2f}",
            extra={"extra": {"event_type": "short_put_selected",
                             "strike": short_strike, "delta": option_data['delta']}}
        )
        self._notify(
            f"✅ Short Put: ${short_strike:.0f} Δ={option_data['delta']:.4f} "
            f"Mid=${option_data['mid']:.2f}"
        )

        # Now search for long put (further OTM, lower absolute delta)
        self._long_put_search_id = self.find_option_by_delta(
            target_delta=self.long_put_delta,
            option_kind=OptionKind.PUT,
            expiry_date=self._1dte_expiry,
            strike_range=40,
            strike_step=5,
            selection_delay_seconds=20.0,
            callback=self._on_long_put_found
        )

    def _on_long_put_found(self, search_id, selected_option, option_data):
        """Callback when long put delta search completes."""
        if not selected_option or not option_data:
            self._abort_entry("No long put found matching target delta")
            return

        long_strike = option_data['strike']

        # Ensure long strike is below short strike
        if self._target_short_strike and long_strike >= self._target_short_strike:
            self._abort_entry(
                f"Long strike ${long_strike:.0f} >= Short strike ${self._target_short_strike:.0f}"
            )
            return

        self._target_long_strike = long_strike
        self._found_legs[long_strike] = selected_option

        self.logger.info(
            f"✅ LONG PUT selected | Strike=${long_strike:.0f} | "
            f"Δ={option_data['delta']:.4f} (target={self.long_put_delta}) | "
            f"IV={option_data['iv']:.2%} Mid=${option_data['mid']:.2f}",
            extra={"extra": {"event_type": "long_put_selected",
                             "strike": long_strike, "delta": option_data['delta']}}
        )
        self._notify(
            f"✅ Long Put: ${long_strike:.0f} Δ={option_data['delta']:.4f} "
            f"Mid=${option_data['mid']:.2f}"
        )

        # Both legs found — create spread
        self._create_spread_instrument()

    def _on_entry_timeout(self, event):
        """Handle overall entry sequence timeout."""
        if not self.entry_in_progress:
            return

        if self.spread_instrument:
            self.logger.info(
                f"⏱️ Entry timeout ({self.entry_timeout_seconds}s) but spread is ready. "
                "Continuing to wait for acceptable quote..."
            )
        else:
            self.logger.info(
                f"⏱️ ENTRY TIMEOUT - Spread Not Ready | Legs found: {len(self._found_legs)}"
            )
            self._abort_entry(f"Entry setup timed out after {self.entry_timeout_seconds}s")

    def _abort_entry(self, reason: str):
        """Abort entry sequence and reset state."""
        self.logger.warning(f"❌ Entry ABORTED: {reason}")
        self._notify(f"❌ Entry ABORTED: {reason}")
        self.entry_in_progress = False
        self._found_legs.clear()
        self._target_short_strike = None
        self._target_long_strike = None

        try:
            self.clock.cancel_timer(f"{self.id}_entry_timeout")
        except Exception:
            pass

        self.save_state()

    # =========================================================================
    # SPREAD CONSTRUCTION & ORDER SUBMISSION
    # =========================================================================

    def _create_spread_instrument(self):
        """Create spread instrument from found legs."""
        short_inst = self._found_legs.get(self._target_short_strike)
        long_inst = self._found_legs.get(self._target_long_strike)

        if not short_inst or not long_inst:
            self._abort_entry("Missing leg instruments")
            return

        # Put Credit Spread: Buy protection (long lower strike), Sell credit (short higher strike)
        legs = [
            (long_inst.id, 1),   # Buy the long put (protection)
            (short_inst.id, -1)  # Sell the short put (credit)
        ]

        self.logger.info(
            f"📦 Creating spread | Long: {long_inst.id} (BUY) | Short: {short_inst.id} (SELL)",
            extra={"extra": {"event_type": "create_spread",
                             "long_leg": str(long_inst.id), "short_leg": str(short_inst.id)}}
        )
        self.create_and_request_spread(legs)

    def on_spread_ready(self, instrument: Instrument):
        """Called when spread instrument is loaded. Entry resumes on ticks."""
        self.logger.info(f"✅ Spread instrument ready: {instrument.id} | Waiting for quote")
        # Entry will happen in on_quote_tick_safe when we get a quote

    def _calculate_entry_price(self, bid: float, ask: float) -> float:
        """Calculate the limit price biased by entry_price_adjustment."""
        spread_width = ask - bid
        if spread_width <= 0.10:
            return ask  # Tight spread — just pay the ask
        return bid + (spread_width * self.entry_price_adjustment)

    def _check_and_submit_entry(self, quote: QuoteTick):
        """Check spread price on tick and submit entry if limit met."""
        if not self.spread_instrument or not self.entry_in_progress:
            return

        bid = quote.bid_price.as_double()
        ask = quote.ask_price.as_double()
        if bid == 0 and ask == 0:
            return

        mid = self._calculate_entry_price(bid, ask)
        credit_received = abs(mid)  # Credit is positive
        credit_dollars = credit_received * 100 * self.config_quantity

        # Validate signal freshness
        if self._signal_time:
            signal_age = (self.clock.utc_now() - self._signal_time).total_seconds()
            if signal_age > self.signal_max_age_seconds:
                self._abort_entry(f"Signal Expired: {signal_age:.1f}s > {self.signal_max_age_seconds}s")
                return

        # Validate minimum credit
        if credit_dollars < self.min_credit_amount:
            now_ts = int(self.clock.utc_now().timestamp())
            if now_ts % 5 == 0 and getattr(self, "_last_wait_log_ts", 0) != now_ts:
                self._last_wait_log_ts = now_ts
                self.logger.info(
                    f"Waiting for price | Bid={bid:.4f} Ask={ask:.4f} "
                    f"Credit=${credit_dollars:.2f} < Min=${self.min_credit_amount:.2f}"
                )
            return

        # For a credit spread, limit price should be negative (credit)
        limit_price = mid
        rounded_limit = self.round_to_tick(limit_price, self.spread_instrument)

        self.logger.info(
            f"📤 ENTRY | Bid={bid:.4f} Ask={ask:.4f} Limit={rounded_limit:.4f} | "
            f"Credit=${credit_received:.4f}/spread (${credit_dollars:.2f} total) | "
            f"Short: ${self._target_short_strike:.0f} | Long: ${self._target_long_strike:.0f}",
            extra={"extra": {"event_type": "entry_submit",
                             "bid": bid, "ask": ask, "limit": rounded_limit,
                             "credit": credit_received, "credit_total": credit_dollars}}
        )

        now_iso = self.clock.utc_now().astimezone(self.tz).isoformat()
        if self._trading_data:
            try:
                self._current_trade_id = self._trading_data.record_trade(
                    strategy_id=self.strategy_id,
                    instrument_id=str(self.spread_instrument.id),
                    trade_type="PUT_CREDIT_SPREAD",
                    direction="SHORT",
                    quantity=self.config_quantity,
                    entry_price=rounded_limit,
                    stop_loss=None,
                    take_profit=None,
                    entry_time=now_iso,
                    raw_data={
                        "short_strike": self._target_short_strike,
                        "long_strike": self._target_long_strike,
                        "credit_per_spread": credit_received,
                        "expiry": "1DTE"
                    }
                )
            except Exception as e:
                self.logger.error(f"Failed to record trade: {e}")

        # Submit order
        success = self.open_spread_position(
            quantity=self.config_quantity,
            is_buy=True,
            limit_price=rounded_limit,
            time_in_force=TimeInForce.DAY
        )

        if success:
            self._spread_entry_price = abs(rounded_limit)
            self.traded_today = True
            self.entry_in_progress = False

            try:
                self.clock.cancel_timer(f"{self.id}_entry_timeout")
            except Exception:
                pass

            # Get entry order ID for fill tracking
            if self.spread_instrument:
                orders = list(self.cache.orders_open(instrument_id=self.spread_instrument.id))
                if orders:
                    self._entry_order_id = orders[-1].client_order_id

            # Set fill timeout
            self.clock.set_time_alert(
                name=f"{self.id}_fill_timeout",
                alert_time=self.clock.utc_now() + timedelta(seconds=self.fill_timeout_seconds),
                callback=self._on_fill_timeout
            )

            self._notify(
                f"📤 ENTRY SUBMITTED | Credit=${abs(rounded_limit):.4f}/spread | "
                f"Short=${self._target_short_strike:.0f} Long=${self._target_long_strike:.0f} | "
                f"Qty={self.config_quantity}"
            )
        else:
            self._abort_entry("Failed to submit spread order")

        self.save_state()

    # =========================================================================
    # POSITION MANAGEMENT — SL/TP
    # =========================================================================

    def on_quote_tick_safe(self, tick: QuoteTick):
        """Handle quote ticks. Route SPX to base, process spread here."""
        super().on_quote_tick_safe(tick)

        if self.spread_instrument and tick.instrument_id == self.spread_instrument.id:
            if self.entry_in_progress:
                self._check_and_submit_entry(tick)
            elif self._spread_entry_price is not None:
                self._manage_open_position()

    def _manage_open_position(self):
        """Monitor spread for stop loss and take profit."""
        if self._spread_entry_price is None or not self.spread_instrument:
            return

        position_qty = self.get_effective_spread_quantity()
        if position_qty == 0:
            return

        quote = self.cache.quote_tick(self.spread_instrument.id)
        if not quote:
            return

        bid = quote.bid_price.as_double()
        ask = quote.ask_price.as_double()
        if bid == 0 and ask == 0:
            return

        mid = (bid + ask) / 2.0
        entry_credit = self._spread_entry_price

        # Current cost to close (absolute)
        current_cost = abs(mid)
        # P&L per spread = credit - cost (positive = profit)
        pnl_per_spread = (entry_credit - current_cost) * 100

        # Calculate SL/TP prices
        sl_debit = entry_credit + (entry_credit * self.stop_loss_pct / 100.0)
        sl_price = -sl_debit  # Negative (debit to close)
        tp_remaining = entry_credit * (1.0 - self.take_profit_pct / 100.0)
        if tp_remaining < 0.05:
            tp_remaining = 0.05
        tp_price = -tp_remaining  # Negative (debit to close)

        # Track max unrealized loss
        total_pnl = pnl_per_spread * abs(position_qty)
        if self._current_trade_id and self._trading_data and total_pnl < 0:
            try:
                self._trading_data.update_max_unrealized_loss(
                    self._current_trade_id, total_pnl
                )
            except Exception:
                pass

        # STOP LOSS — check before closing flag (SL overrides TP)
        if mid <= sl_price and not self._sl_triggered:
            if self.spread_instrument:
                active_orders = list(self.cache.orders_open(
                    instrument_id=self.spread_instrument.id
                ))
                if active_orders:
                    self.logger.warning(
                        f"🛑 SL OVERRIDE | Cancelling {len(active_orders)} pending orders"
                    )
                    self.cancel_all_orders(self.spread_instrument.id)

            self.logger.info(
                f"🛑 STOP LOSS | Mid={mid:.4f} <= SL={sl_price:.4f} | P&L=${total_pnl:.2f}",
                extra={"extra": {"event_type": "stop_loss_trigger",
                                 "mid": mid, "sl_price": sl_price, "pnl": total_pnl}}
            )
            self._notify(f"🛑 STOP LOSS | P&L=${total_pnl:.2f}")
            self._closing_in_progress = True
            self._sl_triggered = True
            sl_limit = mid - 0.05
            self.close_spread_smart(limit_price=sl_limit)
            return

        # Skip TP if already closing
        if self._closing_in_progress:
            return

        # TAKE PROFIT
        if mid >= tp_price:
            self.logger.info(
                f"✅ TAKE PROFIT | Mid={mid:.4f} >= TP={tp_price:.4f} | P&L=${total_pnl:.2f}",
                extra={"extra": {"event_type": "take_profit_trigger",
                                 "mid": mid, "tp_price": tp_price, "pnl": total_pnl}}
            )
            self._notify(f"✅ TAKE PROFIT | P&L=${total_pnl:.2f}")
            self._closing_in_progress = True

            # CRITICAL SAFETY: Cancel any lingering entry orders
            if self.spread_instrument:
                self.cancel_all_orders(self.spread_instrument.id)

            self.close_spread_smart(limit_price=mid)

    # =========================================================================
    # ORDER EVENT HANDLERS
    # =========================================================================

    def on_order_filled_safe(self, event):
        """Handle fills — track commission and detect position close."""
        # Cancel fill timeout on full entry fill
        if self._entry_order_id and event.client_order_id == self._entry_order_id:
            order = self.cache.order(self._entry_order_id)
            if order and order.status == OrderStatus.FILLED:
                try:
                    self.clock.cancel_timer(f"{self.id}_fill_timeout")
                except Exception:
                    pass
                self._entry_order_id = None

        # Track commission (spread fills only, avoid leg double-counting)
        if event.commission:
            try:
                exec_id = getattr(event, "trade_id", None)
                if exec_id and exec_id not in self._processed_executions:
                    self._processed_executions.add(exec_id)
                    try:
                        instrument = self.cache.instrument(event.instrument_id)
                        is_spread = hasattr(instrument, "legs") or type(instrument).__name__ == "OptionSpread"
                    except Exception:
                        is_spread = False
                    if is_spread:
                        comm = event.commission.as_double()
                        self._total_commission += comm
            except Exception as e:
                self.logger.warning(f"Commission tracking error: {e}")

        # Handle close confirmation
        if self._closing_in_progress:
            effective_qty = self.get_effective_spread_quantity()
            if effective_qty == 0:
                self._on_position_closed(event)

    def _on_position_closed(self, event):
        """Handle successful position close."""
        fill_price = 0.0
        order_id_to_check = event.client_order_id
        if "-LEG-" in str(event.client_order_id):
            parent_id_str = str(event.client_order_id).split("-LEG-")[0]
            order_id_to_check = ClientOrderId(parent_id_str)

        tracked_limit = self._active_spread_order_limits.get(order_id_to_check)
        if tracked_limit is not None:
            fill_price = tracked_limit
        else:
            order = self.cache.order(order_id_to_check)
            if order and hasattr(order, "avg_px") and order.avg_px is not None:
                avg_px_val = order.avg_px.as_double() if hasattr(order.avg_px, "as_double") else float(order.avg_px)
                if abs(avg_px_val) <= 5.0:
                    fill_price = avg_px_val
            if fill_price == 0.0:
                fill_price = event.last_px.as_double() if hasattr(event.last_px, "as_double") else float(event.last_px)

        entry_credit = self._spread_entry_price or 0.0
        current_cost = abs(fill_price)
        final_pnl = (entry_credit - current_cost) * 100

        # Determine exit reason
        sl_debit = entry_credit + (entry_credit * self.stop_loss_pct / 100.0)
        sl_price = -sl_debit
        tp_remaining = entry_credit * (1.0 - self.take_profit_pct / 100.0)
        if tp_remaining < 0.05:
            tp_remaining = 0.05
        tp_price = -tp_remaining

        if fill_price <= sl_price:
            exit_reason = "STOP_LOSS"
        elif fill_price >= tp_price:
            exit_reason = "TAKE_PROFIT"
        else:
            exit_reason = "MANUAL"

        now_iso = self.clock.utc_now().astimezone(self.tz).isoformat()

        if self._current_trade_id and self._trading_data:
            try:
                self._trading_data.close_trade(
                    trade_id=self._current_trade_id,
                    exit_price=fill_price, exit_reason=exit_reason,
                    exit_time=now_iso, commission=self._total_commission
                )
                self._trading_data.record_order(
                    strategy_id=self.strategy_id,
                    instrument_id=str(self.spread_instrument.id) if self.spread_instrument else "UNKNOWN",
                    trade_type="PUT_CREDIT_SPREAD", trade_direction="EXIT",
                    order_side="SELL", order_type="LIMIT",
                    quantity=self.config_quantity, status="FILLED",
                    price_limit=fill_price, submitted_time=now_iso,
                    trade_id=self._current_trade_id,
                    client_order_id=f"{self._current_trade_id}-EXIT",
                    filled_time=now_iso, filled_quantity=self.config_quantity,
                    filled_price=fill_price, commission=self._total_commission,
                    raw_data={"trigger": exit_reason, "pnl": final_pnl}
                )
            except Exception as e:
                self.logger.error(f"Failed to record exit: {e}")

        self.logger.info(
            f"✅ POSITION CLOSED | Exit={exit_reason} | PnL=${final_pnl:.2f} | Commission=${self._total_commission:.2f}",
            extra={"extra": {"event_type": "position_closed",
                             "exit_reason": exit_reason, "pnl": final_pnl}}
        )
        self._notify(f"✅ CLOSED | {exit_reason} | PnL=${final_pnl:.2f}")

        # Reset state
        self._spread_entry_price = None
        self._closing_in_progress = False
        self._sl_triggered = False
        self._current_trade_id = None
        self._total_commission = 0.0
        self._active_spread_order_limits.clear()
        self.save_state()

    def _on_fill_timeout(self, event):
        """Handle fill timeout for entry order."""
        if not self._entry_order_id:
            return

        order = self.cache.order(self._entry_order_id)
        if not order:
            return

        if order.status not in [OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED]:
            return

        filled_qty = float(order.filled_qty)

        if filled_qty > 0:
            self.logger.warning(f"⏱️ FILL TIMEOUT | Partial: {filled_qty}/{float(order.quantity)}")
            self._notify(f"⏱️ FILL TIMEOUT | Partial fill: {filled_qty}")
            self.cancel_order(order)
            if self._current_trade_id and self._trading_data:
                self._trading_data.update_trade_quantity(self._current_trade_id, filled_qty)
        else:
            self.logger.warning(f"⏱️ FILL TIMEOUT | No fills in {self.fill_timeout_seconds}s")
            self._notify(f"⏱️ FILL TIMEOUT | No fills, order cancelled")
            self.cancel_order(order)
            self._spread_entry_price = None
            self._entry_order_id = None
            if self._current_trade_id and self._trading_data:
                self._trading_data.delete_trade(self._current_trade_id)
                self._current_trade_id = None
            self.traded_today = True
            self.save_state()

    # --- Close Order Failsafe ---

    def on_order_canceled_safe(self, event):
        self._handle_close_order_failure(event, "CANCELLED")

    def on_order_rejected_safe(self, event):
        self._handle_close_order_failure(event, "REJECTED")

    def on_order_expired_safe(self, event):
        self._handle_close_order_failure(event, "EXPIRED")

    def _handle_close_order_failure(self, event, reason: str):
        """Reset closing flag if close order fails but position remains."""
        if not self._closing_in_progress:
            return
        effective_qty = self.get_effective_spread_quantity()
        if effective_qty == 0:
            return
        self.logger.warning(
            f"⚠️ CLOSE ORDER {reason} | Position still open ({effective_qty:.0f}) | "
            f"Resetting _closing_in_progress"
        )
        self._notify(f"⚠️ CLOSE ORDER {reason} | Position open, SL/TP resumed")
        self._closing_in_progress = False
        self.save_state()

    # =========================================================================
    # STATE MANAGEMENT
    # =========================================================================

    def _reset_daily_state(self, new_date):
        """Reset daily state for new trading day."""
        old_date = self.current_trading_day
        super()._reset_daily_state(new_date)

        self.traded_today = False
        self.entry_in_progress = False
        self._found_legs.clear()
        self._last_log_minute = -1
        self._entry_order_id = None

        try:
            self.clock.cancel_timer(f"{self.id}_fill_timeout")
        except Exception:
            pass
        try:
            self.clock.cancel_timer(f"{self.id}_entry_timeout")
        except Exception:
            pass

        # PRESERVE STATE FOR OVERNIGHT 1DTE POSITIONS
        position_qty = self.get_effective_spread_quantity()
        if position_qty == 0:
            self._spread_entry_price = None
            self._closing_in_progress = False
            self._sl_triggered = False

            # Handle leftover trade from previous day (e.g. cancelled entry)
            if self._current_trade_id and self._trading_data:
                trade_record = self._trading_data.get_trade(self._current_trade_id)
                if trade_record and trade_record.get("status") == "OPEN":
                    if trade_record.get("entry_price") is not None:
                        self._trading_data.close_trade(
                            trade_id=self._current_trade_id,
                            exit_price=0.0, exit_reason="EXPIRED",
                            commission=self._total_commission
                        )
                    else:
                        self._trading_data.delete_trade(self._current_trade_id)
                self._current_trade_id = None
            
            self._total_commission = 0.0
            self._processed_executions = set()
        else:
            self.logger.info(
                f"🌙 OVERNIGHT POSITION | Maintaining state for Day 2 | Qty: {position_qty} | Trade ID: {self._current_trade_id}"
            )

        # Evaluate macro calendar once for the new day
        self._macro_clear_today = self._is_macro_clear()

        self.logger.info(
            f"📅 NEW TRADING DAY: {new_date} | Previous: {old_date} | Macro={'CLEAR' if self._macro_clear_today else 'BLOCKED'}",
            extra={"extra": {"event_type": "new_trading_day", "date": str(new_date), "macro_clear": self._macro_clear_today}}
        )
        self._notify(f"📅 NEW TRADING DAY: {new_date} | Macro={'CLEAR' if self._macro_clear_today else 'BLOCKED'}")

    def get_state(self) -> Dict[str, Any]:
        """Return strategy state for persistence."""
        state = super().get_state()
        state.update({
            "traded_today": self.traded_today,
            "_spread_entry_price": self._spread_entry_price,
            "_target_short_strike": self._target_short_strike,
            "_target_long_strike": self._target_long_strike,
            "_closing_in_progress": self._closing_in_progress,
            "_sl_triggered": self._sl_triggered,
            "_current_trade_id": self._current_trade_id,
            "_total_commission": self._total_commission,
            "_es_ema_value": self._es_ema_value,
            "_es_vwma_value": self._es_vwma_value,
            "_es_sma_value": self._es_sma_value,
            "_es_daily_closes": list(self._es_daily_closes),
            "_es_daily_volumes": list(self._es_daily_volumes),
            "_es_daily_opens": list(self._es_daily_opens),
            "_es_1min_closes": list(self._es_1min_closes),
        })
        return state

    def set_state(self, state: Dict[str, Any]):
        """Restore strategy state."""
        super().set_state(state)
        self.traded_today = state.get("traded_today", False)
        self._spread_entry_price = state.get("_spread_entry_price")
        self._target_short_strike = state.get("_target_short_strike")
        self._target_long_strike = state.get("_target_long_strike")
        self._closing_in_progress = state.get("_closing_in_progress", False)
        self._sl_triggered = state.get("_sl_triggered", False)
        self._current_trade_id = state.get("_current_trade_id")
        self._total_commission = state.get("_total_commission", 0.0)
        self._es_ema_value = state.get("_es_ema_value")
        self._es_vwma_value = state.get("_es_vwma_value")
        self._es_sma_value = state.get("_es_sma_value")

        for c in state.get("_es_daily_closes", []):
            self._es_daily_closes.append(c)
        for v in state.get("_es_daily_volumes", []):
            self._es_daily_volumes.append(v)
        for o in state.get("_es_daily_opens", []):
            self._es_daily_opens.append(o)
        for c in state.get("_es_1min_closes", []):
            self._es_1min_closes.append(c)

        self.logger.info(
            f"State restored | Traded={self.traded_today} | "
            f"Entry={self._spread_entry_price} | "
            f"EMA={self._es_ema_value} VWMA={self._es_vwma_value} SMA={self._es_sma_value} | "
            f"Daily Closes={len(self._es_daily_closes)}",
            extra={"extra": {"event_type": "state_restored"}}
        )

    def on_stop_safe(self):
        """Clean up when strategy stops."""
        position_qty = self.get_effective_spread_quantity()
        self.logger.info(f"🛑 STOPPING | Traded={self.traded_today} | Pos={position_qty}")

        if position_qty != 0:
            self.close_spread_smart()

        # Unsubscribe ES bars
        if self.es_subscribed:
            try:
                self.unsubscribe_bars(self._es_daily_bar_type)
                self.unsubscribe_bars(self._es_1min_bar_type)
                self.es_subscribed = False
            except Exception as e:
                self.logger.error(f"Failed to unsubscribe ES bars: {e}")

        super().on_stop_safe()
        self.logger.info("🛑 SPX1DTEBullPutSpreadStrategy stopped")
