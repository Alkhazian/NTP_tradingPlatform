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
from app.services.trading_data_service import TradingDataService
from app.services.telegram_service import TelegramNotificationService


class SPX1DTEBullPutStrategy(SPXBaseStrategy):
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
        self.commission_per_contract = float(params.get("commission_per_contract", 0.0))

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
        # EMA requires ~250 trading days of history to "warm up" and match TradingView
        self._es_daily_closes: deque = deque(maxlen=300)
        self._es_daily_volumes: deque = deque(maxlen=self.es_vwma_period + 5)
        self._es_daily_opens: deque = deque(maxlen=5)  # For green candle check
        self._es_1min_closes: deque = deque(maxlen=self.es_sma_period + 5)
        self._es_ema_value: Optional[float] = None  # Current EMA(20) value (live, updates intraday)
        self._es_vwma_value: Optional[float] = None  # Current VWMA(14) value (live, updates intraday)
        self._es_sma_value: Optional[float] = None   # Current SMA(10) 1-min value
        self._es_current_price: float = 0.0

        # --- D-1 snapshots (mirrors Pine Script's [1] indexing) ---
        # These are frozen at the close of D-1 and never mutated by live intraday
        # bar updates. Used by _is_strong_reclaim and _is_two_day_confirmed so that
        # the regime filters always compare against the PRIOR day's confirmed values,
        # identical to Pine's: pClose = dClose[1] / pEMA20 = dEMA20[1].
        self._es_d1_close: Optional[float] = None   # D-1 close
        self._es_d1_open:  Optional[float] = None   # D-1 open  (green-candle check)
        self._es_d1_ema:   Optional[float] = None   # EMA(20) as of D-1 close
        self._es_d1_vwma:  Optional[float] = None   # VWMA(14) as of D-1 close
        self._es_d2_close: Optional[float] = None   # D-2 close
        self._es_d2_open:  Optional[float] = None   # D-2 open
        self._es_d2_ema:   Optional[float] = None   # EMA(20) as of D-2 close (two-day check)

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
        self._actual_qty: float = 0.0
        self._last_log_minute: int = -1
        self._last_metrics_update_time: Optional[datetime] = None  # throttle DB writes
        self._last_position_log_time: Optional[datetime] = None   # throttle position status logs
        self._position_log_interval_seconds: int = 30
        self._macro_clear_today: bool = True  # Cached daily; set in _reset_daily_state
        self._ema_ok: bool = False  # Cached; set on ES daily bar (D-1 > EMA)
        self._vwma_ok: bool = False # Cached; set on ES daily bar (D-1 > VWMA)
        self._strong_reclaim_ok: bool = False  # Cached; set on ES daily bar
        self._two_day_confirmed_ok: bool = False  # Cached; set on ES daily bar
        # Day-blocking flag: set True when a HARD daily filter (EMA20, VWMA14, macro, strong
        # reclaim) fails at any minute close. Blocks all further entry checks for the rest of
        # the session. Only resets in _reset_daily_state() on the next trading day.
        self._daily_blocked: bool = False
        # One-shot flag: set True the first time close_price > or_high while hard filters pass.
        # Prevents repeated OR breakout log lines when SMA10 is the only remaining blocker.
        self._or_breakout_logged: bool = False
        
        # Custom status for UI broadcasting
        self._last_position_status: Dict[str, Any] = {}
        self._option_chain_requested_today: bool = False

        # --- Option Search State ---
        self._short_put_search_id: Optional[str] = None
        self._long_put_search_id: Optional[str] = None
        self._found_legs: Dict[float, Instrument] = {}  # strike -> instrument
        self._target_short_strike: Optional[float] = None
        self._target_long_strike: Optional[float] = None

        # --- Services ---
        self._trading_data = TradingDataService(db_path="data/trading.db")
        
        self.telegram = TelegramNotificationService()

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
        if hasattr(self, 'telegram') and self.telegram:
            try:
                self.logger.info(f"Triggering Telegram notification: {message[:50]}...")
                self.telegram.send_message(f"[{self.strategy_id}] {message}")
            except Exception as e:
                self.logger.error(f"Failed to send Telegram notification: {e}")


    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    def on_start_safe(self):
        """Called after primary instrument ready. Subscribe to ES data and warm up option chain."""
        super().on_start_safe()

        params = self.strategy_config.parameters
        
        # Strategy-specific time settings
        start_time_str = params.get("start_time_str", "09:30:03")
        t = datetime.strptime(start_time_str, "%H:%M:%S").time()
        self.start_time = t
        # CRITICAL: Override base class market_open_time to use config value
        # Without this, the range calculation ignores start_time_str!
        self.market_open_time = t

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


        range_end_time = "Range Close" # Will be calculated/logged by base

        self.logger.info(
            f"🚀 {self.strategy_id} STARTED | {self.tz} | Window: {self.start_time}-{range_end_time} | Cutoff: {self.entry_cutoff_time} | "
            f"ES Daily: EMA({self.es_ema_period}), VWMA({self.es_vwma_period}) | ES 1min: SMA({self.es_sma_period})",
            extra={
                "extra": {
                    "event_type": "strategy_start",
                    "strategy": "SPX1DTEBullPutStrategy",
                    "full_config": self.strategy_config.dict(),
                    "timezone": str(self.tz),
                    "start_time": str(self.start_time),
                    "window_minutes": self.opening_range_minutes,
                    "entry_cutoff": str(self.entry_cutoff_time),
                }
            }
        )
        self._notify(
            f"🚀 STARTED | {self.tz} | Window: {self.start_time}-{range_end_time} | Cutoff: {self.entry_cutoff_time} | "
            f"SL={self.stop_loss_pct}% TP={self.take_profit_pct}%"
        )


        # Evaluate hard daily filters 15 seconds after startup.
        # This provides the IB adapter enough time to asynchronously fetch the 365-day backfill
        # before we declare EMA/VWMA missing and permanently block the day.
        from datetime import timedelta
        self.clock.set_time_alert(
            f"{self.id}_startup_block_eval",
            self.clock.utc_now() + timedelta(seconds=15),
            self._evaluate_daily_block_at_open
        )

    def _subscribe_es_data(self):
        """Subscribe to ES futures bar data and request historical backfill."""
        try:
            # Check if ES instrument is in cache
            self.es_instrument = self.cache.instrument(self.es_instrument_id)
            if self.es_instrument:
                self.logger.info(f"ES instrument found in cache: {self.es_instrument_id}")
            else:
                self.logger.info(f"ES instrument not in cache, requesting: {self.es_instrument_id}")
                self.request_instrument(self.es_instrument_id)

            # Only backfill if state wasn't restored with enough data already.
            need_daily_backfill = len(self._es_daily_closes) < self.es_ema_period

            # FORCE backfill if we have bars but the restored state is missing the calculated EMA.
            if not need_daily_backfill and (self._es_d1_ema is None or self._es_d1_vwma is None):
                self.logger.warning(
                    f"State restored {len(self._es_daily_closes)} daily closes, but D1 EMA/VWMA is None. "
                    f"Forcing a fresh backfill to recalculate indicators."
                )
                self._es_daily_closes.clear()
                self._es_daily_opens.clear()
                self._es_daily_volumes.clear()
                self._es_ema_value = None
                self._es_vwma_value = None
                self._es_d1_close = self._es_d1_open = self._es_d1_ema = self._es_d1_vwma = None
                self._es_d2_close = self._es_d2_open = self._es_d2_ema = None
                need_daily_backfill = True

            need_1min_backfill = len(self._es_1min_closes) < self.es_sma_period

            from datetime import timedelta
            now_utc = self.clock.utc_now()

            if need_daily_backfill:
                # Specific contracts like ESH6 typically have ~3 months of history.
                # 60 calendar days ≈ 42 trading days (plenty for EMA20).
                days_back = 60
                
                now_et = now_utc.astimezone(self.tz)
                end_of_yesterday = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
                end_daily = end_of_yesterday.astimezone(pytz.utc)

                self.request_bars(
                    self._es_daily_bar_type,
                    start=end_daily - timedelta(days=days_back),
                    end=end_daily,
                )
                self.logger.info(
                    f"📊 Requesting ES daily backfill | {self._es_daily_bar_type} | "
                    f"Last {days_back} days ending {end_of_yesterday.date()} "
                    f"(have {len(self._es_daily_closes)}/{self.es_ema_period} closes)"
                )
            else:
                self.logger.info(
                    f"📊 Skipping ES daily backfill — state already has "
                    f"{len(self._es_daily_closes)} closes (need {self.es_ema_period})"
                )

            self.subscribe_bars(self._es_daily_bar_type)

            if need_1min_backfill:
                self.request_bars(
                    self._es_1min_bar_type,
                    start=now_utc - timedelta(days=1),
                    end=now_utc,
                )
                self.logger.info(
                    f"📊 Requesting ES 1-min backfill | Last 1 day "
                    f"(have {len(self._es_1min_closes)}/{self.es_sma_period} closes)"
                )
            else:
                self.logger.info(
                    f"📊 Skipping ES 1-min backfill — state already has "
                    f"{len(self._es_1min_closes)} closes (need {self.es_sma_period})"
                )

            self.subscribe_bars(self._es_1min_bar_type)
            self.es_subscribed = True

            self.logger.info(
                f"📊 ES data subscribed | Daily: {self._es_daily_bar_type} | "
                f"1min: {self._es_1min_bar_type} | "
                f"Persisted: {len(self._es_daily_closes)} daily closes, "
                f"{len(self._es_1min_closes)} 1min closes"
            )
        except Exception as e:
            self.logger.error(f"Failed to subscribe to ES data: {e}", exc_info=True)

    # =========================================================================
    # BAR HANDLERS — ES1! INDICATOR ENGINE
    # =========================================================================

    def on_historical_data(self, data):
        """Route historical backfill data to local handlers."""
        try:
            if isinstance(data, Bar):
                self.on_bar(data)
        except Exception as e:
            self.logger.error(f"Error processing historical data: {e}", exc_info=True)

    def on_bar(self, bar: Bar):
        """Route incoming bars to appropriate handler."""
        try:
            if bar.bar_type == self._es_daily_bar_type:
                self._handle_es_daily_bar(bar)
            elif bar.bar_type == self._es_1min_bar_type:
                self._handle_es_1min_bar(bar)
            else:
                self.logger.debug(
                    f"Unrouted bar | type={bar.bar_type} | "
                    f"expected daily={self._es_daily_bar_type} 1min={self._es_1min_bar_type}"
                )
        except Exception as e:
            self.logger.error(f"Error processing bar: {e}", exc_info=True)

    def _handle_es_daily_bar(self, bar: Bar):
        """Process ES daily bar: update EMA(20) and VWMA(14)."""
        close = float(bar.close)
        open_price = float(bar.open)
        volume = float(bar.volume)

        # Snapshot D-1 → becomes D-2 BEFORE appending the new bar.
        # After appending, current bar becomes D-1.
        # This mirrors Pine's [1] indexing: the values stored in _es_d2_* were
        # D-1 at the previous step; the new bar now pushes them back one slot.
        self._es_d2_close = self._es_d1_close
        self._es_d2_open  = self._es_d1_open
        self._es_d2_ema   = self._es_d1_ema  # old D-1 EMA → now D-2 EMA

        self._es_daily_closes.append(close)
        self._es_daily_opens.append(open_price)
        self._es_daily_volumes.append(volume)

        # Update EMA(20)
        self._update_ema()

        # Update VWMA(14)
        self._update_vwma()

        # Freeze D-1 snapshot = this bar's confirmed values.
        # Pine: pClose = dClose[1], pEMA20 = dEMA20[1]
        self._es_d1_close = close
        self._es_d1_open  = open_price
        self._es_d1_ema   = self._es_ema_value   # EMA as of THIS close
        self._es_d1_vwma  = self._es_vwma_value  # VWMA as of THIS close

        ema_str = f"{self._es_ema_value:.2f}" if self._es_ema_value is not None else "N/A"
        vwma_str = f"{self._es_vwma_value:.2f}" if self._es_vwma_value is not None else "N/A"
        self.logger.info(
            f"📊 ES Daily Bar | Close={close:.2f} Open={open_price:.2f} Vol={volume:.0f} | "
            f"EMA({self.es_ema_period})={ema_str} | "
            f"VWMA({self.es_vwma_period})={vwma_str} | "
            f"Bars: {len(self._es_daily_closes)}",
            extra={"extra": {"event_type": "es_daily_bar",
                             "close": close, "ema": self._es_ema_value, "vwma": self._es_vwma_value}}
        )

        # Re-evaluate daily regime filters (only changes on new daily bars)
        # If EMA is not ready yet (warmup period), we allow trading to proceed.
        self._ema_ok = True if self._es_d1_ema is None else (self._es_d1_close > self._es_d1_ema)
        self._vwma_ok = True if self._es_d1_vwma is None else (self._es_d1_close > self._es_d1_vwma)
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

    def _update_ema(self):
        """
        Recalculate or update EMA(N) using standard exponential smoothing.
        If self._es_ema_value is None (e.g. after backfill), it computes the EMA
        by rolling through the ENTIRE deque of historical closes. This "warm-up"
        period (e.g. 250 bars) is required for the EMA to match TradingView.
        """
        n = self.es_ema_period
        closes = list(self._es_daily_closes)
        
        if len(closes) < n:
            self._es_ema_value = None
            return

        multiplier = 2.0 / (n + 1)

        # If we have no prior EMA state, we must roll the EMA from the VERY FIRST
        # bar in our history deque to properly "warm up" the exponential weights.
        if self._es_ema_value is None:
            # Seed the EMA with the SMA of the oldest N bars
            ema = sum(closes[:n]) / n
            # Roll it forward through the rest of the historical bars
            for close_price in closes[n:]:
                ema = (close_price - ema) * multiplier + ema
            self._es_ema_value = ema
        else:
            # We already have a warmed-up EMA; just update it with the newest bar.
            # (Note: the newest bar is closes[-1], which might be a live intraday update)
            newest_close = closes[-1]
            self._es_ema_value = (newest_close - self._es_ema_value) * multiplier + self._es_ema_value

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
    # SL / TP PRICE HELPER
    # =========================================================================

    def _compute_sl_tp_prices(self, entry_credit: float):
        """
        Calculate stop-loss and take-profit price levels from entry credit.

        Returns:
            (sl_price, tp_price) — both negative (debit to close).
        """
        sl_debit = entry_credit + (entry_credit * self.stop_loss_pct / 100.0)
        tp_remaining = entry_credit * (1.0 - self.take_profit_pct / 100.0)
        if tp_remaining < 0.05:
            tp_remaining = 0.05
        return -sl_debit, -tp_remaining

    # =========================================================================
    # TREND FILTER CHECKS
    # =========================================================================


    def _is_strong_reclaim(self) -> bool:
        """
        D-1 regime check: prior day close > D-1 EMA(20) AND green candle.

        Matches Pine Script exactly:
            pClose = dClose[1]
            pEMA20 = dEMA20[1]   ← EMA as of D-1 close, NOT today's live EMA
            pAboveEma    = pClose > pEMA20
            pGreenCandle = pClose > dOpen[1]
            pReclaimOK   = useStrongReclaim ? (pAboveEma and pGreenCandle) : pAboveEma
        """
        if not self.require_strong_reclaim:
            return True

        # Use stable D-1 snapshots frozen at prior session's close.
        # Avoids the ambiguous [-1]/[-2] index (changes once today's live bar
        # arrives) and EMA drift from intraday bar-in-progress updates.
        if self._es_d1_close is None or self._es_d1_ema is None:
            self.logger.debug("D-1 snapshot not ready: bypassing strong reclaim check (warmup)")
            return True

        above_ema    = self._es_d1_close > self._es_d1_ema
        green_candle = self._es_d1_close > (self._es_d1_open or self._es_d1_close)

        if not (above_ema and green_candle):
            self.logger.debug(
                f"Strong reclaim FAILED | "
                f"D1_Close={self._es_d1_close:.2f} D1_Open={self._es_d1_open:.2f} D1_EMA={self._es_d1_ema:.2f} | "
                f"AboveEMA={'✓' if above_ema else '✗'} Green={'✓' if green_candle else '✗'}"
            )
        return above_ema and green_candle

    def _is_two_day_confirmed(self) -> bool:
        """
        2 consecutive daily closes above their respective EMA(20).

        Matches Pine Script:
            pReclaimOK      = pClose[1] > pEMA20[1] and pClose[1] > pOpen[1]
            pReclaim2DayOK  = pReclaimOK and pReclaimOK[1]  (D-1 and D-2 satisfy)
        """
        if not self.require_two_day_confirmation:
            return True

        # If indicators are still warming up, bypass the filter to allow trading
        if self._es_d1_close is None or self._es_d1_ema is None:
            return True
        if self._es_d2_close is None or self._es_d2_ema is None:
            return True

        d1_above_ema = self._es_d1_close > self._es_d1_ema
        d1_green     = self._es_d1_close > (self._es_d1_open or self._es_d1_close)
        d1_ok        = (d1_above_ema and d1_green) if self.require_strong_reclaim else d1_above_ema

        d2_above_ema = self._es_d2_close > self._es_d2_ema
        d2_green     = self._es_d2_close > (self._es_d2_open or self._es_d2_close)
        d2_ok        = (d2_above_ema and d2_green) if self.require_strong_reclaim else d2_above_ema

        return d1_ok and d2_ok

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
    # SPX MINUTE-CLOSE HANDLER — ENTRY + POSITION MANAGEMENT
    # =========================================================================

    def on_spx_tick(self, tick: QuoteTick):
        """Required override — all logic runs in on_minute_closed()."""
        pass

    def on_minute_closed(self, close_price: float):
        """Entry evaluation + periodic heartbeat on each SPX minute close."""
        now_et = self.clock.utc_now().astimezone(self.tz)
        current_minute = now_et.hour * 60 + now_et.minute

        # ── 5-min heartbeat — suppressed when session is already blocked ──────
        # No point logging status every 5 min if we know it's blocked for the day.
        if not self._daily_blocked and current_minute % 5 == 0 and current_minute != self._last_log_minute:
            self._last_log_minute = current_minute
            or_str = f"OR_H={self.or_high:.2f}" if self.or_high else "OR pending"
            ema_str = f"{self._es_d1_ema:.2f}" if self._es_d1_ema is not None else "N/A"
            vwma_str = f"{self._es_d1_vwma:.2f}" if self._es_d1_vwma is not None else "N/A"
            sma_str = f"{self._es_sma_value:.2f}" if self._es_sma_value is not None else "N/A"
            self.logger.info(
                f"📈 SPX={self.current_spx_price:.2f} Close={close_price:.2f} | "
                f"{or_str} | ES={self._es_current_price:.2f} "
                f"EMA={ema_str} VWMA={vwma_str} SMA={sma_str} | "
                f"Traded={self.traded_today} Entry={self.entry_in_progress}"
            )

        # ── Pre-emptive Option Chain Request (TFMITH-style) ──────────────────
        if not self._option_chain_requested_today and not self.traded_today:
            # Calculate range end time
            range_end_total_minutes = self.market_open_time.hour * 60 + self.market_open_time.minute + self.opening_range_minutes
            trigger_total_minutes = range_end_total_minutes - 2
            cutoff_total_minutes = self.entry_cutoff_time.hour * 60 + self.entry_cutoff_time.minute
            
            if trigger_total_minutes <= current_minute <= cutoff_total_minutes:
                target_expiry = self._get_target_expiry()
                self.logger.info(f"🔄 Pre-loading option chain 2 mins before OR ends (Expiry={target_expiry})...")
                self.request_option_chain(target_expiry)
                self._option_chain_requested_today = True

        # ── Entry evaluation ──────────────────────────────────────────────────
        if (self.range_calculated
                and not self.traded_today
                and not self.entry_in_progress
                and not self._closing_in_progress
                and not self._daily_blocked
                and self.get_effective_spread_quantity() == 0):
            self._check_entry_signal(close_price)

        # NOTE: _manage_open_position() is intentionally NOT called here.
        # It fires in on_quote_tick_safe() on every spread quote tick, which is
        # the correct trigger — it reacts to actual spread price changes, not
        # SPX minute closes.

    # =========================================================================
    # ENTRY SIGNAL DETECTION
    # =========================================================================

    def _check_entry_signal(self, close_price: float):
        """
        Evaluate all entry conditions at a minute close.

        Hard daily filters (EMA20, VWMA14, macro, strong reclaim):
          → If any fail, set _daily_blocked=True and stop checking for the rest of the day.
        Soft per-minute filters (SMA10, OR breakout):
          → Log at INFO and return; will re-evaluate at the next minute close.
        """
        now_et = self.clock.utc_now().astimezone(self.tz)

        # ── Time gate (silent) ────────────────────────────────────────────────
        if now_et.time() >= self.entry_cutoff_time:
            return
        if now_et.time() < time(9, 30):
            return

        # ── Hard daily filters — block for the rest of the session on failure ─
        # Pine targets D-1 data for these checks, not intraday ES vs live indicators.
        # These are pre-calculated once per daily bar in _handle_es_daily_bar().
        ema_ready = self._es_d1_ema is not None
        vwma_ready = self._es_d1_vwma is not None
        macro_ok  = self._macro_clear_today
        reclaim_ok = self._strong_reclaim_ok
        two_day_ok = self._two_day_confirmed_ok  # disabled by default; True when disabled

        if not (ema_ready and vwma_ready and macro_ok and reclaim_ok and two_day_ok):
            # Log each failing filter at INFO so it's visible in production logs
            if not ema_ready:
                self.logger.info(
                    f"🚫 DAILY BLOCK | EMA{self.es_ema_period} not ready "
                    f"(need {self.es_ema_period} daily bars, have {len(self._es_daily_closes)})"
                )
            if not vwma_ready:
                self.logger.info(
                    f"🚫 DAILY BLOCK | VWMA{self.es_vwma_period} not ready "
                    f"(need {self.es_vwma_period} daily bars)"
                )
            if not macro_ok:
                self.logger.info(
                    f"🚫 DAILY BLOCK | Macro filter | Today is a restricted trading date"
                )
            if not reclaim_ok:
                self.logger.info(
                    f"🚫 DAILY BLOCK | Strong reclaim failed | "
                    f"D-1 close must be above EMA{self.es_ema_period} and a green candle"
                )
            if not two_day_ok:
                self.logger.info(
                    f"🚫 DAILY BLOCK | Two-day confirmation failed | "
                    f"D-1 and D-2 closes must both be above EMA{self.es_ema_period}"
                )
            self._daily_blocked = True
            self._notify(
                f"🚫 STRATEGY BLOCKED FOR DAY | "
                f"EMA Ready={'✓' if ema_ready else '✗'} VWMA Ready={'✓' if vwma_ready else '✗'} "
                f"Macro={'✓' if macro_ok else '✗'} Reclaim={'✓' if reclaim_ok else '✗'} "
                f"Two-Day={'✓' if two_day_ok else '✗'}"
            )
            self.save_state()
            return

        # ── OR breakout: minute close must be ABOVE OR high ───────────────────
        # More conservative than tick-based check: filters wick spikes that reverse.
        if self.or_high is None or close_price <= self.or_high:
            current_str = f"{close_price:.2f}" if self.or_high else "OR not locked"
            or_str = f"{self.or_high:.2f}" if self.or_high else "N/A"
            self.logger.info(
                f"⏳ WAITING | OR breakout not confirmed | "
                f"Close={current_str} ≤ OR_High={or_str}"
            )
            return

        # Proposal B: one-shot log the first time close exceeds OR high
        if not self._or_breakout_logged:
            self._or_breakout_logged = True
            self.logger.info(
                f"📊 OR BREAKOUT CONFIRMED | Close={close_price:.2f} > OR={self.or_high:.2f} | "
                f"Hard filters all passed | Waiting for EMA{self.es_ema_period}, VWMA{self.es_vwma_period}, SMA{self.es_sma_period} confirmation",
                extra={"extra": {"event_type": "or_breakout_confirmed",
                                 "close": close_price, "or_high": self.or_high}}
            )

        # ── EMA, VWMA, SMA10 (1-min) — soft filters, re-evaluates each minute ─────────────
        es_price = self._es_current_price

        if self._es_d1_ema is None or es_price <= self._es_d1_ema:
            ema_str = f"{self._es_d1_ema:.2f}" if self._es_d1_ema else "not ready"
            self.logger.info(
                f"⏳ WAITING | Daily EMA{self.es_ema_period} not met | "
                f"ES(1m)={es_price:.2f} vs Daily EMA={ema_str} | Will retry next minute"
            )
            return

        if self._es_d1_vwma is None or es_price <= self._es_d1_vwma:
            vwma_str = f"{self._es_d1_vwma:.2f}" if self._es_d1_vwma else "not ready"
            self.logger.info(
                f"⏳ WAITING | Daily VWMA{self.es_vwma_period} not met | "
                f"ES(1m)={es_price:.2f} vs Daily VWMA={vwma_str} | Will retry next minute"
            )
            return

        if self._es_sma_value is None or es_price <= self._es_sma_value:
            sma_str = f"{self._es_sma_value:.2f}" if self._es_sma_value else "not ready"
            self.logger.info(
                f"⏳ WAITING | SMA{self.es_sma_period}(1m) not met | "
                f"ES(1m)={es_price:.2f} vs SMA={sma_str} | Will retry next minute"
            )
            return

        # ── All conditions met ─────────────────────────────────────────────────
        self.logger.info(
            f"🔥 ALL ENTRY CONDITIONS MET | "
            f"SPX Close={close_price:.2f} > OR={self.or_high:.2f} | "
            f"ES={es_price:.2f} > Daily EMA={self._es_d1_ema:.2f} "
            f"Daily VWMA={self._es_d1_vwma:.2f} SMA(1m)={self._es_sma_value:.2f}",
            extra={"extra": {"event_type": "entry_signal",
                             "spx_close": close_price, "or_high": self.or_high,
                             "es_price": es_price}}
        )
        self._notify(
            f"🔥 ENTRY SIGNAL | SPX Close={close_price:.2f} > OR={self.or_high:.2f} | "
            f"ES={es_price:.2f}"
        )
        self._initiate_entry()

    # =========================================================================
    # ENTRY SEQUENCE — DELTA-BASED OPTION SEARCH
    # =========================================================================

    def _get_target_expiry(self) -> str:
        """
        Calculate the 1DTE expiry date (next trading day).
        Returns: Expiry date in 'YYYYMMDD' format.
        """
        today = self.clock.utc_now().astimezone(self.tz).date()
        tomorrow = today + timedelta(days=1)
        
        # Skip weekends
        if tomorrow.weekday() == 5:  # Saturday
            tomorrow += timedelta(days=2)
        elif tomorrow.weekday() == 6:  # Sunday
            tomorrow += timedelta(days=1)
            
        return tomorrow.strftime("%Y%m%d")

    def _initiate_entry(self):
        """Start the entry sequence: search for put options by delta."""
        self.entry_in_progress = True
        # NOTE: traded_today is intentionally NOT set here.
        # It is only set in _check_and_submit_entry() on successful order submission.
        
        # Store signal time as UTC so age comparison in _check_and_submit_entry
        # (which uses clock.utc_now()) is always apples-to-apples.
        self._signal_time = self.clock.utc_now()
        self._found_legs.clear()
        
        # Set absolute timeout for entry setup/process
        self.clock.set_time_alert(
            name=f"{self.id}_entry_timeout",
            alert_time=self.clock.utc_now() + timedelta(seconds=self.entry_timeout_seconds),
            callback=self._on_entry_timeout
        )
        
        self.save_state()

        # Calculate 1DTE expiry
        self._1dte_expiry = self._get_target_expiry()

        self.logger.info(
            f"📋 Starting entry sequence | 1DTE Expiry: {self._1dte_expiry} | "
            f"Short Δ={self.short_put_delta} Long Δ={self.long_put_delta}"
        )

        # Search for BOTH legs in parallel (cutting setup time in half)
        self.find_options_by_deltas(
            target_deltas=[self.short_put_delta, self.long_put_delta],
            option_kind=OptionKind.PUT,
            expiry_date=self._1dte_expiry,
            selection_delay_seconds=10.0,
            callback=self._on_spread_legs_found
        )

    def _on_spread_legs_found(self, search_id, selected_options, options_data):
        """Callback when multi-delta search completes for both legs."""
        if not self.entry_in_progress:
            return

        if len(selected_options) < 2 or any(opt is None for opt in selected_options):
            self._abort_entry("Failed to find both legs for the bull put spread")
            return

        # Short put is first in target_deltas list
        short_opt = selected_options[0]
        short_data = options_data[0]
        short_strike = short_data['strike']
        
        # Long put is second
        long_opt = selected_options[1]
        long_data = options_data[1]
        long_strike = long_data['strike']

        # Ensure long strike is below short strike (safety check)
        if long_strike >= short_strike:
            self._abort_entry(
                f"Invalid Spread: Long strike ${long_strike:.0f} >= Short strike ${short_strike:.0f}"
            )
            return

        # Handle Short leg
        self._target_short_strike = short_strike
        self._found_legs[short_strike] = short_opt
        self.logger.info(
            f"✅ SHORT PUT selected | Strike=${short_strike:.0f} | "
            f"Δ={short_data['delta']:.4f} (target={self.short_put_delta}) | "
            f"IV={short_data['iv']:.2%} | Mid=${short_data['mid']:.2f}",
            extra={"extra": {"event_type": "short_put_selected",
                             "strike": short_strike, "delta": short_data['delta'],
                             "iv": short_data['iv'], "mid": short_data['mid']}}
        )
        self._notify(
            f"✅ Short Put: ${short_strike:.0f} Δ={short_data['delta']:.4f} "
            f"IV={short_data['iv']:.2%} Mid=${short_data['mid']:.2f}"
        )

        # Handle Long leg
        self._target_long_strike = long_strike
        self._found_legs[long_strike] = long_opt
        self.logger.info(
            f"✅ LONG PUT selected | Strike=${long_strike:.0f} | "
            f"Δ={long_data['delta']:.4f} (target={self.long_put_delta}) | "
            f"IV={long_data['iv']:.2%} | Mid=${long_data['mid']:.2f}",
            extra={"extra": {"event_type": "long_put_selected",
                             "strike": long_strike, "delta": long_data['delta'],
                             "iv": long_data['iv'], "mid": long_data['mid']}}
        )
        self._notify(
            f"✅ Long Put: ${long_strike:.0f} Δ={long_data['delta']:.4f} "
            f"IV={long_data['iv']:.2%} Mid=${long_data['mid']:.2f}"
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

        # Cancel any active delta searches to prevent timer/subscription leaks.
        # This handles the case where the short put was found but the long put
        # search is still running when we abort (e.g. on entry timeout).
        for sid in list(self._premium_searches.keys()):
            self.cancel_premium_search(sid)

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
                short_strike = self._target_short_strike
                long_strike = self._target_long_strike
                entry_premium = abs(rounded_limit) * 100  # dollars per spread

                # SL/TP price levels for the trade record
                entry_stop_loss, entry_target_price = self._compute_sl_tp_prices(abs(rounded_limit))

                max_profit = entry_premium * self.config_quantity
                spread_width_pts = abs(short_strike - long_strike) if short_strike and long_strike else 50
                max_loss = (spread_width_pts * 100 - entry_premium) * self.config_quantity

                strikes_list = (
                    [f"{int(short_strike)}P", f"{int(long_strike)}P"]
                    if short_strike and long_strike else None
                )
                legs_info = [
                    {"strike": short_strike, "side": "SELL", "type": "P"},
                    {"strike": long_strike, "side": "BUY", "type": "P"}
                ] if short_strike and long_strike else None

                strategy_config_snapshot = {
                    "stop_loss_pct": self.stop_loss_pct,
                    "take_profit_pct": self.take_profit_pct,
                    "min_credit_amount": self.min_credit_amount,
                    "quantity": self.config_quantity,
                    "short_put_delta": self.short_put_delta,
                    "long_put_delta": self.long_put_delta,
                    "require_strong_reclaim": self.require_strong_reclaim,
                    "require_two_day_confirmation": self.require_two_day_confirmation,
                    "enable_macro_filter": self.enable_macro_filter,
                }

                trade_date_str = self.clock.utc_now().astimezone(self.tz).strftime("%Y%m%d")
                trade_time_str = self.clock.utc_now().astimezone(self.tz).strftime("%H%M%S")
                self._current_trade_id = f"T-1DTE-{trade_date_str}-{trade_time_str}"

                self._trading_data.start_trade(
                    trade_id=self._current_trade_id,
                    strategy_id=self.strategy_id,
                    instrument_id=str(self.spread_instrument.id),
                    trade_type="PUT_CREDIT_SPREAD",
                    entry_price=rounded_limit,
                    quantity=self.config_quantity,
                    direction="LONG",
                    entry_time=now_iso,
                    entry_reason={
                        "trigger": "OR_HIGH_BREAKOUT",
                        "short_strike": short_strike,
                        "long_strike": long_strike,
                        "credit_per_spread": abs(rounded_limit),
                        "expiry": "1DTE",
                    },
                    entry_target_price=entry_target_price,
                    entry_stop_loss=entry_stop_loss,
                    strikes=strikes_list,
                    expiration=self.clock.utc_now().astimezone(self.tz).strftime("%Y-%m-%d"),
                    legs=legs_info,
                    strategy_config=strategy_config_snapshot,
                    max_profit=max_profit,
                    max_loss=max_loss,
                    entry_premium_per_contract=entry_premium,
                )

                # Record entry order
                self._trading_data.record_order(
                    strategy_id=self.strategy_id,
                    instrument_id=str(self.spread_instrument.id),
                    trade_type="PUT_CREDIT_SPREAD",
                    trade_direction="ENTRY",
                    order_side="BUY",
                    order_type="LIMIT",
                    quantity=self.config_quantity,
                    status="SUBMITTED",
                    submitted_time=now_iso,
                    trade_id=self._current_trade_id,
                    client_order_id=f"{self._current_trade_id}-ENTRY",
                    price_limit=rounded_limit,
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

            # Start periodic fill-wait monitoring every 10s (mirrors template pattern).
            # _log_fill_wait_status reschedules itself and stops when _entry_order_id clears.
            try:
                self.clock.set_time_alert(
                    name=f"{self.id}_fill_wait_monitor",
                    alert_time=self.clock.utc_now() + timedelta(seconds=10),
                    callback=self._log_fill_wait_status
                )
            except Exception as e:
                self.logger.warning(f"Failed to set fill wait monitor: {e}")

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
        sl_price, tp_price = self._compute_sl_tp_prices(entry_credit)

        # Track trade metrics (max drawdown, P&L snapshots) — throttled to 30s
        total_pnl = pnl_per_spread * abs(position_qty)
        now_utc = self.clock.utc_now()
        if self._current_trade_id and self._trading_data:
            should_update = (
                self._last_metrics_update_time is None
                or (now_utc - self._last_metrics_update_time).total_seconds() >= 30
            )
            if should_update:
                try:
                    self._trading_data.update_trade_metrics(
                        self._current_trade_id, total_pnl
                    )
                    self._last_metrics_update_time = now_utc
                except Exception:
                    pass

        # Periodic position status log (every 30s) — matches template pattern
        should_log = (
            self._last_position_log_time is None
            or (now_utc - self._last_position_log_time).total_seconds() >= self._position_log_interval_seconds
        )
        if should_log:
            self._last_position_log_time = now_utc
            if total_pnl > 0:
                health = "🟢 PROFIT"
            elif total_pnl > -50:
                health = "🟡 SLIGHT LOSS"
            else:
                health = "🔴 LOSS"
            distance_to_sl = mid - sl_price    # Positive = still above SL
            distance_to_tp = tp_price - mid    # Positive = still below TP (further to go)
            self.logger.info(
                f"📊 POSITION STATUS | {health} | Qty: {abs(position_qty):.0f} | "
                f"P&L: ${total_pnl:+.2f} | Mid: {mid:.4f} | Bid: {bid:.4f} | Ask: {ask:.4f} | "
                f"Entry: {entry_credit:.4f} | SL: {sl_price:.4f} ({distance_to_sl:+.4f}) | "
                f"TP: {tp_price:.4f} ({distance_to_tp:+.4f})",
                extra={"extra": {
                    "event_type": "position_status",
                    "health": health,
                    "quantity": abs(position_qty),
                    "pnl_total": total_pnl,
                    "current_mid": mid,
                    "current_bid": bid,
                    "current_ask": ask,
                    "entry_credit": entry_credit,
                    "sl_price": sl_price,
                    "tp_price": tp_price,
                    "distance_sl": distance_to_sl,
                    "distance_tp": distance_to_tp,
                }}
            )

        # Update custom status for UI broadcasting
        # We update this every tick, not just when logging, to ensure UI is fresh
        if self.spread_instrument:
            self._last_position_status = {
                "symbol": str(self.spread_instrument.id),
                "health": "🟢 PROFIT" if total_pnl > 0 else "🔴 LOSS" if total_pnl < -50 else "🟡 SLIGHT LOSS",
                "quantity": abs(position_qty),
                "pnl": total_pnl,
                "mid": mid,
                "bid": bid,
                "ask": ask,
                "entry": entry_credit,
                "sl": sl_price,
                "tp": tp_price,
                "last_update": now_utc.isoformat()
            }

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

            # BUGFIX: Use tp_price as limit (not mid). The TP trigger condition is
            # mid >= tp_price, so tp_price is the cheapest debit we'll accept to close.
            # Using mid would submit an order at a potentially stale price; using tp_price
            # guarantees we close at-or-better-than the configured TP threshold.
            self.close_spread_smart(limit_price=tp_price)

    # =========================================================================
    # ORDER EVENT HANDLERS
    # =========================================================================

    def on_order_filled_safe(self, event):
        """Handle fills - track commission and detect position close."""
        # Cancel fill timeout on full entry fill
        if self._entry_order_id and event.client_order_id == self._entry_order_id:
            order = self.cache.order(self._entry_order_id)
            if order and order.status == OrderStatus.FILLED:
                try:
                    self.clock.cancel_timer(f"{self.id}_fill_timeout")
                except Exception:
                    pass
                try:
                    self.clock.cancel_timer(f"{self.id}_fill_wait_monitor")
                except Exception:
                    pass
                self._entry_order_id = None
                
                # Capture actual quantity for commission calculation later
                if self._current_trade_id:
                    fills_data = self.get_accumulated_spread_price(str(event.client_order_id))
                    if fills_data:
                        self._actual_qty = float(fills_data["total_qty"])
                        self.logger.info(f"🔄 Entry quantity captured: {self._actual_qty} lots")

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
                # Spread credit/debit prices are normally < $5.00 for this strategy.
                # If avg_px > 5.00, it's likely a bug or a raw aggregate strike price
                # rather than the actual filled option premium. Fall back to event.last_px.
                MAX_EXPECTED_SPREAD_PRICE = 5.0
                if abs(avg_px_val) <= MAX_EXPECTED_SPREAD_PRICE:
                    fill_price = avg_px_val
            if fill_price == 0.0:
                fill_price = event.last_px.as_double() if hasattr(event.last_px, "as_double") else float(event.last_px)

        entry_credit = self._spread_entry_price or 0.0
        current_cost = abs(fill_price)
        final_pnl = (entry_credit - current_cost) * 100

        # Determine exit reason
        sl_price, tp_price = self._compute_sl_tp_prices(entry_credit)

        if fill_price <= sl_price:
            exit_reason = "STOP_LOSS"
        elif fill_price >= tp_price:
            exit_reason = "TAKE_PROFIT"
        else:
            exit_reason = "MANUAL"

        now_iso = self.clock.utc_now().astimezone(self.tz).isoformat()

        # Calculate commission using simplified model
        commission = round(self.commission_per_contract * self._actual_qty, 2)

        if self._current_trade_id and self._trading_data:
            try:
                self._trading_data.close_trade(
                    trade_id=self._current_trade_id,
                    exit_price=fill_price, exit_reason=exit_reason,
                    exit_time=now_iso, commission=commission
                )
                self._trading_data.record_order(
                    strategy_id=self.strategy_id,
                    instrument_id=str(self.spread_instrument.id) if self.spread_instrument else "UNKNOWN",
                    trade_type="PUT_CREDIT_SPREAD", trade_direction="EXIT",
                    order_side="SELL", order_type="LIMIT",
                    quantity=self._actual_qty,
                    status="FILLED",
                    price_limit=fill_price, submitted_time=now_iso,
                    trade_id=self._current_trade_id,
                    client_order_id=f"{self._current_trade_id}-EXIT",
                    filled_time=now_iso,
                    filled_quantity=self._actual_qty,
                    filled_price=fill_price, commission=commission,
                    raw_data={"trigger": exit_reason, "pnl": final_pnl}
                )
            except Exception as e:
                self.logger.error(f"Failed to record exit: {e}")

        self.logger.info(
            f"✅ POSITION CLOSED | Exit={exit_reason} | PnL=${final_pnl:.2f} | Commission=${commission:.2f} ({self._actual_qty} contracts @ ${self.commission_per_contract:.2f})",
            extra={"extra": {"event_type": "position_closed",
                             "exit_reason": exit_reason, "pnl": final_pnl}}
        )
        self._notify(f"✅ CLOSED | {exit_reason} | PnL=${final_pnl:.2f}")

        # Reset state
        self._spread_entry_price = None
        self._closing_in_progress = False
        self._sl_triggered = False
        self.entry_in_progress = False  # Safety: clear in case of unexpected re-entry race
        self._current_trade_id = None
        self._actual_qty = 0.0
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
            self._entry_order_id = None
            try:
                self.clock.cancel_timer(f"{self.id}_fill_wait_monitor")
            except Exception:
                pass
            if self._current_trade_id and self._trading_data:
                self._trading_data.update_trade_quantity(self._current_trade_id, filled_qty)
                self._actual_qty = filled_qty
        else:
            self.logger.warning(f"⏱️ FILL TIMEOUT | No fills in {self.fill_timeout_seconds}s")
            self._notify(f"⏱️ FILL TIMEOUT | No fills, order cancelled")
            self.cancel_order(order)
            self._spread_entry_price = None
            self._entry_order_id = None
            try:
                self.clock.cancel_timer(f"{self.id}_fill_wait_monitor")
            except Exception:
                pass
            if self._current_trade_id and self._trading_data:
                self._trading_data.delete_trade(self._current_trade_id)
                self._current_trade_id = None
            self.traded_today = True
            self.save_state()

    def _log_fill_wait_status(self, event):
        """
        Log spread quote every 10s while waiting for entry fill.
        Ported from SPX_15Min_Range template.
        Stops automatically when _entry_order_id is cleared (full fill or timeout).
        """
        # Stop if entry order has already been resolved (filled or timed out)
        if not self._entry_order_id:
            return

        if self.spread_instrument:
            quote = self.cache.quote_tick(self.spread_instrument.id)
            if quote:
                bid = quote.bid_price.as_double()
                ask = quote.ask_price.as_double()
                mid = (bid + ask) / 2.0
                limit = -self._spread_entry_price if self._spread_entry_price else None
                distance = round(mid - limit, 4) if limit is not None else None
                self.logger.info(
                    f"⏳ WAITING FOR FILL | Bid: {bid:.4f} | Ask: {ask:.4f} | Mid: {mid:.4f} "
                    f"| Limit: {limit:.4f if limit is not None else 'N/A'} "
                    f"| Distance: {f'{distance:+.4f}' if distance is not None else 'N/A'} "
                    f"| Order: {self._entry_order_id}",
                    extra={"extra": {
                        "event_type": "fill_wait_status",
                        "bid": bid, "ask": ask, "mid": mid,
                        "limit_price": limit, "distance_to_limit": distance,
                        "order_id": str(self._entry_order_id)
                    }}
                )
            else:
                self.logger.info(
                    f"⏳ WAITING FOR FILL | No quote available | Order: {self._entry_order_id}",
                    extra={"extra": {"event_type": "fill_wait_status",
                                     "order_id": str(self._entry_order_id)}}
                )

        # Reschedule next check in 10s.
        # Cancel first: NautilusTrader holds the timer name until the callback returns,
        # so we must cancel before re-registering the same name.
        try:
            self.clock.cancel_timer(f"{self.id}_fill_wait_monitor")
        except Exception:
            pass
        self.clock.set_time_alert(
            name=f"{self.id}_fill_wait_monitor",
            alert_time=self.clock.utc_now() + timedelta(seconds=10),
            callback=self._log_fill_wait_status
        )

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
        self._signal_time = None
        self._target_short_strike = None
        self._target_long_strike = None
        self._last_metrics_update_time = None
        self._last_position_log_time = None
        # Reset day-block and OR-breakout flags for the new session
        self._daily_blocked = False
        self._or_breakout_logged = False

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
                            commission=0.0
                        )
                    else:
                        self._trading_data.delete_trade(self._current_trade_id)
                self._current_trade_id = None

            self._actual_qty = 0.0
        else:
            self.logger.info(
                f"🌙 OVERNIGHT POSITION | Maintaining state for Day 2 | Qty: {position_qty} | Trade ID: {self._current_trade_id}"
            )

        # Reset flags
        self._option_chain_requested_today = False
        
        # Evaluate macro calendar once for the new day
        self._macro_clear_today = self._is_macro_clear()

        self.logger.info(
            f"📅 NEW TRADING DAY: {new_date} | Previous: {old_date} | Macro={'CLEAR' if self._macro_clear_today else 'BLOCKED'}",
            extra={"extra": {"event_type": "new_trading_day", "date": str(new_date), "macro_clear": self._macro_clear_today}}
        )
        self._notify(f"📅 NEW TRADING DAY: {new_date} | Macro={'CLEAR' if self._macro_clear_today else 'BLOCKED'}")

        # ── Early-day hard-filter evaluation ─────────────────────────────────
        # Check EMA20, VWMA14, strong reclaim, and two-day confirmation right
        # at the start of the session (using yesterday's closed daily bar data).
        # If any hard filter fails NOW, block the day immediately so we never
        # reach _check_entry_signal() unnecessarily.
        self._evaluate_daily_block_at_open()

    def _evaluate_daily_block_at_open(self, event=None):
        """
        Run hard daily filter checks at the start of each trading day.
        Sets _daily_blocked=True immediately if EMA/VWMA are not ready, strong reclaim, 
        macro filter, or two-day confirmation is not satisfied. This avoids the first call to
        _check_entry_signal() having to discover the block.
        Called at the end of _reset_daily_state() and from on_start_safe() (via timer).
        """
        fail_reasons = []
        warnings = []
        
        # Block if indicators are not ready yet
        if self._es_d1_ema is None:
            fail_reasons.append(f"EMA{self.es_ema_period} not ready ({len(self._es_daily_closes)}/{self.es_ema_period} bars)")
            
        if self._es_d1_vwma is None:
            fail_reasons.append(f"VWMA{self.es_vwma_period} not ready ({len(self._es_daily_closes)}/{self.es_vwma_period} bars)")
            
        if not self._macro_clear_today:
            fail_reasons.append("Macro event date")
        if not self._strong_reclaim_ok:
            fail_reasons.append(f"Strong reclaim not met")
        if not self._two_day_confirmed_ok:
            fail_reasons.append(f"Two-day confirmation not met")

        if fail_reasons:
            self._daily_blocked = True
            reasons_str = " | ".join(fail_reasons)
            self.logger.info(
                f"🚫 DAILY BLOCK AT OPEN | {reasons_str}",
                extra={"extra": {"event_type": "daily_block_at_open", "reasons": fail_reasons}}
            )
            self._notify(
                f"🚫 BLOCKED FOR DAY | {reasons_str}"
            )
            self.save_state()
        else:
            ema_str = f"{self._es_d1_ema:.2f}" if self._es_d1_ema is not None else "N/A"
            vwma_str = f"{self._es_d1_vwma:.2f}" if self._es_d1_vwma is not None else "N/A"
            warn_str = f" | ⚠️ {' | '.join(warnings)}" if warnings else ""
            self.logger.info(
                f"✅ DAILY FILTERS OK | D1_EMA={ema_str} D1_VWMA={vwma_str} "
                f"Macro=CLEAR Reclaim=OK TwoDay=OK{warn_str}",
                extra={"extra": {"event_type": "daily_filters_ok", "warnings": warnings}}
            )

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
            "_actual_qty": self._actual_qty,
            "_es_ema_value": self._es_ema_value,
            "_es_vwma_value": self._es_vwma_value,
            "_es_sma_value": self._es_sma_value,
            "_es_daily_closes": list(self._es_daily_closes),
            "_es_daily_volumes": list(self._es_daily_volumes),
            "_es_daily_opens": list(self._es_daily_opens),
            "_es_1min_closes": list(self._es_1min_closes),
            "_daily_blocked": self._daily_blocked,
            "_or_breakout_logged": self._or_breakout_logged,
            "_ema_ok": self._ema_ok,
            "_vwma_ok": self._vwma_ok,
            "_strong_reclaim_ok": self._strong_reclaim_ok,
            "_two_day_confirmed_ok": self._two_day_confirmed_ok,
            # D-1 snapshots (Pine pClose[1] / pEMA20[1] equivalents)
            "_es_d1_close": self._es_d1_close,
            "_es_d1_open":  self._es_d1_open,
            "_es_d1_ema":   self._es_d1_ema,
            "_es_d1_vwma":  self._es_d1_vwma,
            "_es_d2_close": self._es_d2_close,
            "_es_d2_open":  self._es_d2_open,
            "_es_d2_ema":   self._es_d2_ema,
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
        self._actual_qty = state.get("_actual_qty", 0.0)
        self._es_ema_value = state.get("_es_ema_value")
        self._es_vwma_value = state.get("_es_vwma_value")
        self._es_sma_value = state.get("_es_sma_value")
        self._daily_blocked = state.get("_daily_blocked", False)
        self._or_breakout_logged = state.get("_or_breakout_logged", False)
        self._ema_ok = state.get("_ema_ok", False)
        self._vwma_ok = state.get("_vwma_ok", False)
        self._strong_reclaim_ok = state.get("_strong_reclaim_ok", False)
        self._two_day_confirmed_ok = state.get("_two_day_confirmed_ok", False)
        # D-1 snapshots
        self._es_d1_close = state.get("_es_d1_close")
        self._es_d1_open  = state.get("_es_d1_open")
        self._es_d1_ema   = state.get("_es_d1_ema")
        self._es_d1_vwma  = state.get("_es_d1_vwma")
        self._es_d2_close = state.get("_es_d2_close")
        self._es_d2_open  = state.get("_es_d2_open")
        self._es_d2_ema   = state.get("_es_d2_ema")

        for c in state.get("_es_daily_closes", []):
            self._es_daily_closes.append(c)
        for v in state.get("_es_daily_volumes", []):
            self._es_daily_volumes.append(v)
        for o in state.get("_es_daily_opens", []):
            self._es_daily_opens.append(o)
        for c in state.get("_es_1min_closes", []):
            self._es_1min_closes.append(c)

        d1_ema_str = f"{self._es_d1_ema:.2f}" if self._es_d1_ema is not None else "N/A"
        self.logger.info(
            f"State restored | Traded={self.traded_today} | "
            f"Entry={self._spread_entry_price} | "
            f"EMA={self._es_ema_value} VWMA={self._es_vwma_value} SMA={self._es_sma_value} | "
            f"Daily Closes={len(self._es_daily_closes)} | "
            f"D1_Close={self._es_d1_close} D1_EMA={d1_ema_str}",
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

    def get_custom_status(self) -> Dict[str, Any]:
        """Return the latest position status for UI broadcasting."""
        # Only return status if we actually have an active position
        if self.get_effective_spread_quantity() != 0:
            return self._last_position_status
        return {}
