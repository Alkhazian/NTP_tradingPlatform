"""
TFMITH (The First Million Is The Hardest) BleedingEdge Strategy

Intraday directional momentum breakout system for QQQ (configurable).
Monitors the underlying from market open, enters 0DTE long calls/puts
at MARKET after a minimum directional move.

Inherits BaseStrategy directly — NOT SPXBaseStrategy.
"""

import logging
import math
import uuid
from datetime import datetime, date, time as dtime, timedelta
from typing import Dict, Any, Optional, List, Callable

import pytz
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.enums import OptionKind, OrderSide, TimeInForce, OrderStatus

from app.strategies.base import BaseStrategy
from app.services.telegram_service import TelegramNotificationService

IB_VENUE = Venue("IB")

# Maximum option candidates to subscribe for delta search (IB data limit protection)
MAX_DELTA_CANDIDATES = 20


class TFMITHStrategy(BaseStrategy):
    """
    TFMITH BleedingEdge — Intraday directional momentum breakout.

    Monitors a configurable underlying (default QQQ) from market open.
    After start_time, if the underlying has moved >= entry_threshold_pct
    from the opening price, enters a 0DTE long call (bullish) or long put
    (bearish) at MARKET.

    Position sizing is dynamic based on loss_streak and current_allocation.
    Trades are managed with profit targets, soft time stops, and hard time stops.
    """

    def __init__(self, config, integration_manager=None, persistence_manager=None):
        super().__init__(config, integration_manager, persistence_manager)

        params = config.parameters or {}
        self.tz = pytz.timezone(params.get("timezone", "America/New_York"))

        # ── Underlying config ────────────────────────────────────────────────
        self.underlying_symbol = params.get("underlying_symbol", "QQQ")
        self.exchange = params.get("exchange", "SMART")
        self.primary_exchange = params.get("primary_exchange", "NASDAQ")

        # ── Position sizing array ────────────────────────────────────────────
        self.position_size_0 = float(params.get("position_size_0", 10.0))
        self.position_size_1 = float(params.get("position_size_1", 30.0))
        self.position_size_2 = float(params.get("position_size_2", 50.0))
        self.position_size_3 = float(params.get("position_size_3", 100.0))
        self.position_size_4 = float(params.get("position_size_4", 0.1))

        # ── Option config ────────────────────────────────────────────────────
        self.option_delta = float(params.get("option_delta", 0.45))
        self.dte = int(params.get("dte", 0))
        self.commission_per_contract = float(params.get("commission_per_contract", 0.65))

        # ── Entry config ─────────────────────────────────────────────────────
        self.entry_threshold_pct = float(params.get("entry_threshold_pct", 0.25))

        # ── Time config ──────────────────────────────────────────────────────
        start_time_str = params.get("start_time", "10:10")
        soft_end_str = params.get("soft_end_time", "11:00")
        hard_end_str = params.get("hard_end_time", "14:00")
        market_open_str = params.get("start_time_str", "09:30:03")

        self.start_time = datetime.strptime(start_time_str, "%H:%M").time()
        self.soft_end_time = datetime.strptime(soft_end_str, "%H:%M").time()
        self.hard_end_time = datetime.strptime(hard_end_str, "%H:%M").time()
        self.market_open_time = datetime.strptime(market_open_str, "%H:%M:%S").time()

        # ── Profit targets ───────────────────────────────────────────────────
        self.profit_target_pct = float(params.get("profit_target_pct", 30.0))
        self.soft_profit_target_pct = float(params.get("soft_profit_target_pct", 5.0))
        self.soft_profit_flag = bool(params.get("soft_profit_flag", False))

        # ── Allocation ───────────────────────────────────────────────────────
        self.initial_allocation = float(params.get("allocation", 10000.0))

        # ── Persistent state (survives restarts + daily resets) ───────────────
        self.loss_streak: int = 0
        self.current_allocation: float = self.initial_allocation

        # ── Daily state (reset each day) ─────────────────────────────────────
        self.traded_today: bool = False
        self.opening_price: Optional[float] = None
        self.change_since_open: float = 0.0
        self.position_open: bool = False
        self.entry_time: Optional[str] = None
        self.entry_price: Optional[float] = None
        self.option_type: Optional[str] = None    # "CALL" or "PUT" → stored as trade_type in DB
        self.trade_direction: Optional[str] = None  # "LONG" or "SHORT"
        self.actual_position_size: int = 0
        self.current_option_id: Optional[str] = None

        # ── Internal tracking ────────────────────────────────────────────────
        self.underlying_subscribed: bool = False
        self.underlying_instrument: Optional[Instrument] = None
        self.underlying_instrument_id: Optional[InstrumentId] = None
        self.current_underlying_price: float = 0.0
        self.last_underlying_bid: float = 0.0
        self.last_underlying_ask: float = 0.0

        self.current_trading_day: Optional[date] = None
        self._last_minute: int = -1
        self._option_chain_loaded: bool = False

        # Entry flow
        self.entry_in_progress: bool = False
        self._closing_in_progress: bool = False
        self._current_trade_id: Optional[str] = None
        self._entry_order_id = None
        self._exit_order_id = None
        self._actual_qty: float = 0.0
        self._total_commission: float = 0.0
        self._last_position_log_time = None
        self._position_log_interval_seconds: int = 30
        self._last_position_status: Dict[str, Any] = {}

        # Private order tracking — NOT added to BaseStrategy's _pending_entry_orders
        # so BaseStrategy._on_entry_filled never fires (same pattern as SPX strategies)
        self._tfmith_entry_orders: Set = set()
        self._tfmith_exit_orders: Set = set()

        # Partial fill accumulators — accumulate qty and weighted avg price
        # across multiple PARTIALLY_FILLED events before the final FILLED event
        self._entry_fill_qty: float = 0.0   # running sum of entry partial fills
        self._entry_fill_wavg: float = 0.0  # weighted avg price for entry fills
        self._exit_fill_qty: float = 0.0    # running sum of exit partial fills
        self._exit_fill_wavg: float = 0.0   # weighted avg price for exit fills

        # Option search state (analogous to base_spx._premium_searches)
        self._delta_searches: Dict[str, Dict] = {}

        # Option instrument for active position
        self._option_instrument: Optional[Instrument] = None
        self._option_instrument_id: Optional[InstrumentId] = None

        # Telegram
        self._telegram = TelegramNotificationService()


        # TradingDataService
        self._trading_data = None
        if integration_manager:
            self._trading_data = getattr(integration_manager, 'trading_data_service', None)

    # =========================================================================
    # NOTIFICATIONS
    # =========================================================================

    def _notify(self, message: str):
        """Send Telegram notification (fire-and-forget)."""
        if self._telegram:
            try:
                self._telegram.send_message(f"[{self.strategy_id}] {message}")
            except Exception as e:
                self.logger.error(f"Failed to send Telegram: {e}")

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    def _request_instrument(self):
        """
        Override BaseStrategy to explicitly request the underlying STK from IB
        if it's not already in the cache.
        """
        self.instrument = self.cache.instrument(self.instrument_id)
        
        if self.instrument is not None:
            self.logger.info(f"Underlying {self.underlying_symbol} found in cache.")
            self._on_instrument_ready()
        else:
            self.logger.info(
                f"Underlying {self.underlying_symbol} not in cache, requesting STK from IB..."
            )
            # Explicitly request the stock contract
            self.request_instruments(
                venue=IB_VENUE,
                update_catalog=True,
                params={
                    "update_catalog": True,
                    "ib_contracts": ({
                        "secType": "STK",
                        "symbol": self.underlying_symbol,
                        "exchange": self.exchange,
                        "primaryExchange": self.primary_exchange,
                    },)
                }
            )
            # Set timeout for instrument availability securely
            self.clock.set_time_alert(
                name=f"{self.id}.instrument_timeout",
                alert_time=self.clock.utc_now() + timedelta(seconds=60),
                callback=self._on_instrument_timeout
            )

    def on_start_safe(self):
        """Called after primary instrument ready. Subscribe to underlying + option chain."""
        self.logger.info(
            f"🚀 TFMITH starting | Symbol={self.underlying_symbol} | "
            f"Delta={self.option_delta} | Threshold={self.entry_threshold_pct}% | "
            f"LossStreak={self.loss_streak} | Allocation=${self.current_allocation:.2f}"
        )
        self._notify(
            f"🚀 STARTED | {self.tz} | Symbol={self.underlying_symbol} | "
            f"Delta={self.option_delta} | Threshold={self.entry_threshold_pct}% | "
            f"LossStreak={self.loss_streak} | Allocation=${self.current_allocation:.2f}"
        )
        #self._noify(f"✅ Underlying {self.underlying_symbol} ready")
        
        # Link our custom underlying vars to the loaded base instrument
        self.underlying_instrument = self.instrument
        self.underlying_instrument_id = self.instrument.id
        self.underlying_subscribed = True
        # Option chain will be requested 2 minutes before start time
        pass
    def _subscribe_data(self):
        """Subscribe to quote ticks for the primary instrument."""
        self.subscribe_quote_ticks(self.instrument_id)

    # =========================================================================
    # OPTION CHAIN (analogous to SPXBaseStrategy.request_option_chain)
    # =========================================================================

    def _request_option_chain(self):
        """Request option chain for the underlying with target DTE."""
        now = self.clock.utc_now()
        expiry_date = (now.date() + timedelta(days=self.dte)).strftime("%Y%m%d")
        self.logger.info(
            f"📋 Requesting option chain | {self.underlying_symbol} | Expiry={expiry_date}"
        )

        try:
            self.request_instruments(
                venue=IB_VENUE,
                update_catalog=True,
                params={
                    "update_catalog": True,
                    "ib_contracts": ({
                        "secType": "STK",
                        "symbol": self.underlying_symbol,
                        "exchange": self.exchange,
                        "primaryExchange": self.primary_exchange,
                        "build_options_chain": True,
                        "lastTradeDateOrContractMonth": expiry_date,
                    },)
                }
            )
            self._option_chain_loaded = True
        except Exception as e:
            self.logger.error(f"Failed to request option chain: {e}")

    # =========================================================================
    # TICK HANDLING (analogous to SPXBaseStrategy.on_quote_tick_safe)
    # =========================================================================

    def on_quote_tick_safe(self, tick: QuoteTick):
        """Process incoming quote ticks."""
        tick_instrument_id = tick.instrument_id

        # ── Underlying ticks ─────────────────────────────────────────────────
        if (self.underlying_instrument_id and
                tick_instrument_id == self.underlying_instrument_id):
            bid = float(tick.bid_price)
            ask = float(tick.ask_price)
            if bid > 0 and ask > 0:
                self.last_underlying_bid = bid
                self.last_underlying_ask = ask
                self.current_underlying_price = (bid + ask) / 2.0
                self._process_underlying_tick(tick)

        # ── Option ticks (monitor open position) ─────────────────────────────
        elif (self._option_instrument_id and
                tick_instrument_id == self._option_instrument_id and
                self.position_open and not self._closing_in_progress):
            self._monitor_position(tick)

    # =========================================================================
    # MINUTE-CLOSE EMULATION
    # (analogous to SPXBaseStrategy._process_spx_tick_unified)
    # =========================================================================

    def _process_underlying_tick(self, tick: QuoteTick):
        """Emulate 1-minute bar close from ticks. Handles daily reset."""
        now_utc = self.clock.utc_now()
        now_et = now_utc.astimezone(self.tz)
        current_date = now_et.date()
        current_time = now_et.time()
        current_minute = now_et.hour * 60 + now_et.minute

        # ── Daily reset ──────────────────────────────────────────────────────
        if self.current_trading_day is None or current_date != self.current_trading_day:
            self._reset_daily_state(current_date)

        # ── Opening price capture ────────────────────────────────────────────
        if self.opening_price is None and current_time >= self.market_open_time:
            self.opening_price = self.current_underlying_price
            self.logger.info(
                f"📍 Opening price captured: ${self.opening_price:.2f} "
                f"at {now_et.strftime('%H:%M:%S')}"
            )

        # ── Update change since open ─────────────────────────────────────────
        if self.opening_price is not None and self.opening_price > 0:
            self.change_since_open = (
                (self.current_underlying_price - self.opening_price)
                / self.opening_price * 100
            )

        # ── Minute boundary detection ────────────────────────────────────────
        if current_minute != self._last_minute and self._last_minute >= 0:
            self.on_minute_closed(
                close_price=self.current_underlying_price,
                current_time=current_time,
            )

        self._last_minute = current_minute

    # =========================================================================
    # DAILY RESET
    # =========================================================================

    def _reset_daily_state(self, current_date):
        """Reset daily variables. Preserve persistent state."""
        self.logger.info(
            f"📅 NEW TRADING DAY: {current_date} | "
            f"LossStreak={self.loss_streak} | Alloc=${self.current_allocation:.2f}"
        )

        self.current_trading_day = current_date

        # ── Clear daily state ────────────────────────────────────────────────
        # If we have an open position from yesterday, preserve it
        has_overnight = self.position_open and self.entry_price is not None

        if has_overnight:
            self.logger.info(
                f"⚠️ Overnight position detected | Dir={self.trade_direction} | "
                f"Entry=${self.entry_price} | Qty={self.actual_position_size}"
            )
            # Keep: position_open, entry_time, entry_price, trade_direction,
            #        actual_position_size, current_option_id
            self.traded_today = False  # Allow monitoring, prevent new entry
        else:
            self.traded_today = False
            self.opening_price = None
            self.change_since_open = 0.0
            self.position_open = False
            self.entry_time = None
            self.entry_price = None
            self.trade_direction = None
            self.actual_position_size = 0
            self.current_option_id = None

        self.entry_in_progress = False
        self._closing_in_progress = False
        self._last_minute = -1

        # Reset partial fill accumulators on daily reset
        self._entry_fill_qty = 0.0
        self._entry_fill_wavg = 0.0
        self._exit_fill_qty = 0.0
        self._exit_fill_wavg = 0.0

        # ── Persistent state NOT cleared ─────────────────────────────────────
        # loss_streak and current_allocation survive

        self.save_state()
        
        # Reset the flag so the chain requests again 2 minutes before today's start_time
        self._option_chain_requested_today = False
    # =========================================================================
    # SCANNER (on_minute_closed → _check_entry)
    # =========================================================================

    def on_minute_closed(self, close_price: float, current_time: dtime):
        """Called on each minute close. Runs scanner and monitor."""
        # ── Option Chain Pre-load ────────────────────────────────────────────
        if not getattr(self, '_option_chain_requested_today', False):
            current_minutes = current_time.hour * 60 + current_time.minute
            start_minutes = self.start_time.hour * 60 + self.start_time.minute
            
            # Request 2 minutes before start_time (or immediately if started late)
            if current_minutes >= start_minutes - 2:
                self.logger.info(f"🔄 Pre-loading option chain 2 mins before start_time ({self.start_time})...")
                self._request_option_chain()
                self._option_chain_requested_today = True

        # ── Monitor existing position ────────────────────────────────────────
        if self.position_open and not self._closing_in_progress:
            self._check_monitor_exits(current_time)
            return

        # ── Scanner for new entry ────────────────────────────────────────────
        if (not self.traded_today
                and not self.entry_in_progress
                and not self.position_open
                and self.opening_price is not None):
            self._check_entry(close_price, current_time)

    def _check_entry(self, close_price: float, current_time: dtime):
        """
        Scanner: Check if conditions are met for entry.
        
        Check 1: Time gate (start_time <= now <= soft_end_time)
        Check 2: traded_today == False
        Check 3: Directional bias (change_since_open >= threshold)
        """
        # ── Check 1: Time gate ───────────────────────────────────────────────
        if current_time < self.start_time or current_time > self.soft_end_time:
            return

        # ── Check 2: Already traded ──────────────────────────────────────────
        if self.traded_today:
            return

        # ── Check 3: Directional bias ────────────────────────────────────────
        if self.change_since_open >= self.entry_threshold_pct:
            # Bullish: Long Call
            self.logger.info(
                f"🟢 BULLISH SIGNAL | Change={self.change_since_open:.3f}% "
                f">= {self.entry_threshold_pct}% | Price=${close_price:.2f}"
            )
            self._initiate_entry("CALL")

        elif self.change_since_open <= -self.entry_threshold_pct:
            # Bearish: Long Put
            self.logger.info(
                f"🔴 BEARISH SIGNAL | Change={self.change_since_open:.3f}% "
                f"<= -{self.entry_threshold_pct}% | Price=${close_price:.2f}"
            )
            self._initiate_entry("PUT")

    # =========================================================================
    # ENTRY FLOW
    # =========================================================================

    def _initiate_entry(self, direction: str):
        """Start the entry process: find option by delta, then size and execute."""
        self.entry_in_progress = True
        self.option_type = direction      # "CALL" or "PUT"
        self.trade_direction = "LONG"     # TFMITH always buys

        option_kind = OptionKind.CALL if direction == "CALL" else OptionKind.PUT
        # For puts, use negative delta; for calls, positive
        target_delta = self.option_delta if direction == "CALL" else -self.option_delta

        self.logger.info(
            f"🔍 Finding {direction} option | Target Δ={target_delta} | "
            f"DTE={self.dte} | LossStreak={self.loss_streak}"
        )
        self._notify(
            f"🔍 {direction} entry signal | Change={self.change_since_open:.2f}% | "
            f"Finding Δ={target_delta} option"
        )

        search_id = self._find_option_by_delta(
            target_delta=target_delta,
            option_kind=option_kind,
            callback=self._on_option_found,
        )

        if search_id is None:
            self.logger.warning("Delta search failed to start — no candidates")
            self.entry_in_progress = False
            self.traded_today = True  # Block re-entry
            return

        # Set entry timeout
        self.clock.set_time_alert(
            name=f"{self.id}.entry_timeout",
            alert_time=self.clock.utc_now() + timedelta(seconds=60),
            callback=self._on_entry_timeout,
        )

    def _on_option_found(self, search_id: str, option: Optional[Instrument],
                         stats: Optional[Dict]):
        """Callback after delta search completes."""
        if option is None:
            self.logger.warning(
                f"❌ No option found for {self.trade_direction} | "
                f"Aborting entry"
            )
            self._notify(f"❌ No option found — entry aborted")
            self.entry_in_progress = False
            self.traded_today = True
            self.save_state()
            return

        self._option_instrument = option
        self._option_instrument_id = option.id
        self.current_option_id = str(option.id)

        # Get mid price for sizing
        quote = self.cache.quote_tick(option.id)
        if not quote:
            self.logger.warning("No quote for selected option — aborting")
            self.entry_in_progress = False
            self.traded_today = True
            self.save_state()
            return

        bid = float(quote.bid_price)
        ask = float(quote.ask_price)
        mid = (bid + ask) / 2.0

        self.logger.info(
            f"✅ Option found: {option.id} | "
            f"Δ={stats['delta']:.4f} | Mid=${mid:.2f} | "
            f"Strike=${stats['strike']:.2f}"
        )

        # ── Sizing ───────────────────────────────────────────────────────────
        contracts = self._calculate_position_size(mid)

        if contracts < 1:
            self.logger.warning(
                f"❌ Position size too small: {contracts} contracts | "
                f"Allocation=${self.current_allocation:.2f} | "
                f"Aborting"
            )
            self._notify(f"❌ Sizing abort — 0 contracts")
            self.entry_in_progress = False
            self.traded_today = True
            self.save_state()
            return

        # ── Execute entry ────────────────────────────────────────────────────
        self._execute_entry(option, contracts, mid)

    def _calculate_position_size(self, option_price: float) -> int:
        """
        Calculate number of contracts based on loss_streak and allocation.
        
        position_size_{loss_streak} gives % of current_allocation to use.
        contracts = floor(target_amount / (option_price * 100))
        """
        streak = min(self.loss_streak, 4)
        pct_map = {
            0: self.position_size_0,
            1: self.position_size_1,
            2: self.position_size_2,
            3: self.position_size_3,
            4: self.position_size_4,
        }
        pct = pct_map[streak]
        target_amount = self.current_allocation * (pct / 100.0)
        contracts = math.floor(target_amount / (option_price * 100))

        self.logger.info(
            f"📊 Sizing | Streak={streak} → {pct}% | "
            f"Target=${target_amount:.2f} | OptionPx=${option_price:.2f} | "
            f"Contracts={contracts}"
        )
        return contracts

    def _execute_entry(self, option: Instrument, contracts: int, mid_price: float):
        """Submit MARKET order for the option."""
        self.traded_today = True
        self.position_open = True
        self.actual_position_size = contracts
        self.entry_time = self.clock.utc_now().isoformat()

        # Subscribe to option quotes for monitoring
        self.subscribe_quote_ticks(option.id)

        # Create MARKET order
        qty = option.make_qty(contracts)
        order = self.order_factory.market(
            instrument_id=option.id,
            order_side=OrderSide.BUY,
            quantity=qty,
            time_in_force=TimeInForce.DAY,
        )

        self._entry_order_id = order.client_order_id
        self._tfmith_entry_orders.add(order.client_order_id)  # private — bypasses BaseStrategy routing

        self.logger.info(
            f"📈 ENTRY ORDER | {self.option_type} {contracts}x {option.id} | "
            f"MARKET | Mid=${mid_price:.2f}"
        )
        self._notify(
            f"📈 ENTRY | {self.option_type} {contracts}x | "
            f"Mid=${mid_price:.2f} | Streak={self.loss_streak}"
        )

        # DB recording is handled by BaseStrategy._start_trade_record_async on fill.
        # Do NOT record here — that caused the duplicate record.

        self.submit_order(order)
        self.entry_in_progress = False
        self.save_state()

    def _on_entry_timeout(self, event):
        """Cancel entry if option search is still in progress."""
        if self.entry_in_progress:
            self.logger.warning("⏰ Entry timeout — aborting")
            self.entry_in_progress = False
            self.traded_today = True
            self.save_state()

    # =========================================================================
    # OPTION DELTA SEARCH
    # (adapted from SPXBaseStrategy.find_option_by_delta)
    # =========================================================================

    def _find_option_by_delta(
        self,
        target_delta: float,
        option_kind: OptionKind,
        callback: Optional[Callable] = None,
        selection_delay_seconds: float = 12.0,
    ) -> Optional[str]:
        """
        Find option with target delta from cached instruments.
        
        Limits subscription to MAX_DELTA_CANDIDATES closest-to-ATM strikes
        to protect IB data limits.
        """
        search_id = str(uuid.uuid4())
        now = self.clock.utc_now()
        expiry_date = (now.date() + timedelta(days=self.dte)).strftime("%Y%m%d")
        expiry_short = expiry_date[2:] if len(expiry_date) == 8 else expiry_date

        self.logger.info(
            f"🔍 Delta Search {search_id[:8]} | Target Δ={target_delta} | "
            f"Kind={option_kind} | Expiry={expiry_date}"
        )

        # Store search state
        self._delta_searches[search_id] = {
            'search_id': search_id,
            'target_delta': target_delta,
            'option_kind': option_kind,
            'expiry_date': expiry_date,
            'callback': callback,
            'received_options': [],
            'subscribed_instrument_ids': [],
            'active': True,
        }

        # Filter cached instruments for matching options
        all_instruments = self.cache.instruments()
        candidates = []
        current_price = self.current_underlying_price

        for inst in all_instruments:
            if not hasattr(inst, 'option_kind') or inst.option_kind != option_kind:
                continue

            symbol = str(inst.id.symbol)
            if not symbol.startswith(self.underlying_symbol):
                continue
                
            # SAFETY FILTER: Only trade standard 100-multiplier options.
            # Corporate actions (splits/dividends) can create non-standard options
            # (e.g. multiplier exactly 133 or 105). Those break our sizing math.
            if float(inst.multiplier) != 100.0:
                continue

            # Check expiry match
            inst_expiry = ""
            if hasattr(inst, 'expiry'):
                inst_expiry = str(inst.expiry)
            elif hasattr(inst, 'last_trade_date'):
                inst_expiry = str(inst.last_trade_date)

            if expiry_date not in inst_expiry and expiry_short not in symbol:
                continue

            strike = float(inst.strike_price.as_double())
            candidates.append((abs(strike - current_price), inst))

        if not candidates:
            self.logger.warning(
                f"❌ Delta search {search_id[:8]}: NO options for "
                f"{self.underlying_symbol} expiry {expiry_date}"
            )
            self._delta_searches.pop(search_id, None)
            if callback:
                callback(search_id, None, None)
            return None

        # Sort by proximity to ATM, take MAX_DELTA_CANDIDATES closest
        candidates.sort(key=lambda x: x[0])
        selected_candidates = candidates[:MAX_DELTA_CANDIDATES]

        for _, inst in selected_candidates:
            self.subscribe_quote_ticks(inst.id)
            self._delta_searches[search_id]['received_options'].append(inst)
            self._delta_searches[search_id]['subscribed_instrument_ids'].append(inst.id)

        self.logger.info(
            f"📡 Delta search {search_id[:8]}: subscribed to "
            f"{len(selected_candidates)}/{len(candidates)} candidates "
            f"(closest to ATM=${current_price:.2f})"
        )

        # Set selection timer
        timer_name = f"{self.id}.delta_search.{search_id}"
        self.clock.set_time_alert(
            name=timer_name,
            alert_time=self.clock.utc_now() + timedelta(seconds=selection_delay_seconds),
            callback=self._on_delta_search_complete,
        )

        return search_id

    def _on_delta_search_complete(self, timer_event):
        """Select best delta match after quotes have populated."""
        timer_name = timer_event.name if hasattr(timer_event, 'name') else str(timer_event)
        parts = timer_name.rsplit('.', 1)
        if len(parts) < 2:
            self.logger.error(f"Invalid delta search timer: {timer_name}")
            return

        search_id = parts[-1]
        state = self._delta_searches.get(search_id)
        if not state or not state.get('active'):
            return

        state['active'] = False
        received_options = state['received_options']
        target_delta = state['target_delta']
        callback = state['callback']
        subscribed_ids = state.get('subscribed_instrument_ids', [])

        self._delta_searches.pop(search_id, None)

        if not received_options:
            self.logger.warning(f"Delta search {search_id[:8]}: no options to evaluate")
            self._unsubscribe_option_candidates(subscribed_ids, keep=None)
            if callback:
                callback(search_id, None, None)
            return

        abs_target = abs(target_delta)
        candidates = []

        for option in received_options:
            try:
                # Use Nautilus greeks_calculator
                greeks_data = None
                if hasattr(self, 'greeks') and self.greeks:
                    greeks_data = self.greeks.instrument_greeks(option.id)

                quote = self.cache.quote_tick(option.id)
                if not quote:
                    continue

                bid = float(quote.bid_price)
                ask = float(quote.ask_price)
                if bid <= 0 or ask <= 0:
                    continue

                mid = (bid + ask) / 2.0
                spread = ask - bid
                strike = float(option.strike_price.as_double())

                # If greeks available, use them; otherwise estimate delta from moneyness
                if greeks_data and greeks_data.delta is not None:
                    delta = float(greeks_data.delta)
                else:
                    # Rough delta approximation based on moneyness
                    # For calls: ITM → ~1.0, ATM → ~0.5, OTM → ~0.0
                    # For puts: ITM → ~-1.0, ATM → ~-0.5, OTM → ~0.0
                    moneyness = (self.current_underlying_price - strike) / self.current_underlying_price
                    if state['option_kind'] == OptionKind.CALL:
                        delta = max(0.01, min(0.99, 0.5 + moneyness * 5))
                    else:
                        delta = -max(0.01, min(0.99, 0.5 - moneyness * 5))

                abs_delta = abs(delta)

                candidates.append({
                    'option': option,
                    'delta': delta,
                    'abs_delta': abs_delta,
                    'mid': mid,
                    'spread': spread,
                    'strike': strike,
                    'bid': bid,
                    'ask': ask,
                })

            except Exception as e:
                self.logger.warning(f"Evaluation failed for {option.id}: {e}")

        if not candidates:
            self.logger.warning(
                f"Delta search {search_id[:8]}: no valid candidates after evaluation"
            )
            self._unsubscribe_option_candidates(subscribed_ids, keep=None)
            if callback:
                callback(search_id, None, None)
            return

        # Sort by proximity to target delta
        candidates.sort(key=lambda x: abs(x['abs_delta'] - abs_target))

        # Log top candidates
        self.logger.info(f"📊 Top candidates for search {search_id[:8]}:")
        for i, c in enumerate(candidates[:5]):
            dist = abs(c['abs_delta'] - abs_target)
            self.logger.info(
                f"  {i + 1}. Strike ${c['strike']:.2f} | Δ={c['delta']:.4f} | "
                f"Dist={dist:.4f} | Mid=${c['mid']:.2f}"
            )

        best = candidates[0]
        selected_option = best['option']

        # Unsubscribe from all except selected
        self._unsubscribe_option_candidates(
            subscribed_ids, keep=selected_option.id
        )

        self.logger.info(
            f"✅ Delta search {search_id[:8]} selected: "
            f"Strike=${best['strike']:.2f} Δ={best['delta']:.4f} "
            f"(target={target_delta:.3f}) Mid=${best['mid']:.2f}"
        )

        # Fire callback
        if callback:
            callback(search_id, selected_option, best)

    def _unsubscribe_option_candidates(
        self,
        subscribed_ids: List[InstrumentId],
        keep: Optional[InstrumentId] = None,
    ):
        """Unsubscribe from option quotes we no longer need."""
        for inst_id in subscribed_ids:
            if keep and inst_id == keep:
                continue
            # Don't unsubscribe from our active position's option
            if self._option_instrument_id and inst_id == self._option_instrument_id:
                continue
            try:
                self.unsubscribe_quote_ticks(inst_id)
            except Exception:
                pass

    # =========================================================================
    # MONITOR ENGINE
    # 4-check structure per requirements
    # =========================================================================

    def _check_monitor_exits(self, current_time: dtime):
        """
        Monitor engine — called on each minute close when position is open.

        Check 1: Soft Time Stop (now >= soft_end_time)
        Check 2: Hard Time Stop (now >= hard_end_time)
        Check 3: Profit Target (PnL% >= profit_target_pct)
        Check 4: Soft Profit Target (PnL% >= soft_profit_target_pct)
        """
        pnl_pct = self._get_position_pnl_pct()

        # ── Check 3 (Profit Target) — always active ─────────────────────────
        if pnl_pct >= self.profit_target_pct:
            self.logger.info(
                f"🎯 PROFIT TARGET HIT | PnL={pnl_pct:.1f}% >= {self.profit_target_pct}%"
            )
            self._close_position("PROFIT_TARGET")
            return

        # ── Check 1 (Soft Time Stop) ─────────────────────────────────────────
        if current_time >= self.soft_end_time:
            if not self.soft_profit_flag:
                # Option 2: Close immediately
                self.logger.info(
                    f"⏰ SOFT TIME STOP | PnL={pnl_pct:.1f}% | soft_profit_flag=false"
                )
                self._close_position("SOFT_TIME_STOP")
                return
            else:
                # Option 1: Check 4 (Soft Profit Target)
                if pnl_pct >= self.soft_profit_target_pct:
                    self.logger.info(
                        f"⏰ SOFT PROFIT STOP | PnL={pnl_pct:.1f}% >= "
                        f"{self.soft_profit_target_pct}%"
                    )
                    self._close_position("SOFT_PROFIT_TARGET")
                    return
                else:
                    # Fall through to Check 2 (Hard Time Stop)
                    pass

        # ── Check 2 (Hard Time Stop) ─────────────────────────────────────────
        if current_time >= self.hard_end_time:
            self.logger.info(
                f"🛑 HARD TIME STOP | PnL={pnl_pct:.1f}%"
            )
            self._close_position("HARD_TIME_STOP")
            return

        # ── Position status logging (throttled) ──────────────────────────────
        # Logging moved to _monitor_position (tick-driven) for better frequency and data accuracy.
        pass

    def _monitor_position(self, tick: QuoteTick):
        """Called on every option tick — updates UI status and logs status."""
        bid = float(tick.bid_price)
        ask = float(tick.ask_price)
        if bid <= 0 or ask <= 0:
            return

        mid = (bid + ask) / 2.0

        # Update UI status
        if self.entry_price is not None:
            pnl_dollars = (mid - self.entry_price) * 100 * self.actual_position_size
            pnl_pct = ((mid - self.entry_price) / self.entry_price * 100) if self.entry_price > 0 else 0

            # Determine health indicator 🟢/🟡/🔴
            if pnl_pct >= 2:
                health = "🟢 PROFIT"
            elif pnl_pct >= -5.0:
                health = "🟡 SLIGHT LOSS"
            else:
                health = "🔴 LOSS"

            # Delta (if available)
            delta_str = ""
            if hasattr(self, 'greeks') and self.greeks:
                try:
                    greeks_data = self.greeks.instrument_greeks(tick.instrument_id)
                    if greeks_data and greeks_data.delta is not None:
                        delta_str = f" | Δ={float(greeks_data.delta):.3f}"
                except Exception:
                    pass

            # Update UI state
            self._last_position_status = {
                "symbol": self.current_option_id,
                "quantity": self.actual_position_size,
                "entry": self.entry_price,
                "mid": mid,
                "bid": bid,
                "ask": ask,
                "pnl": pnl_dollars,
                "pnl_pct": pnl_pct,
                "direction": self.trade_direction,
                "health": health,
                "underlying_price": self.current_underlying_price,
                "delta": delta_str.strip(" | Δ=") if delta_str else None
            }

            # Periodic position status logging (30 seconds)
            now = self.clock.utc_now()
            if (self._last_position_log_time is None or
                    (now - self._last_position_log_time).total_seconds() >= self._position_log_interval_seconds):
                self._last_position_log_time = now
                
                self.logger.info(
                    f"📊 POSITION | {health} | {self.trade_direction} {self.actual_position_size}x | "
                    f"Entry=${self.entry_price:.2f} | Mid=${mid:.2f} | "
                    f"PnL=${pnl_dollars:+.2f} ({pnl_pct:+.1f}%) | "
                    f"{self.underlying_symbol}=${self.current_underlying_price:.2f}{delta_str}"
                )

    def _get_position_pnl_pct(self) -> float:
        """Get current position PnL as percentage."""
        if not self._option_instrument_id or not self.entry_price:
            return 0.0

        quote = self.cache.quote_tick(self._option_instrument_id)
        if not quote:
            return 0.0

        bid = float(quote.bid_price)
        ask = float(quote.ask_price)
        if bid <= 0 or ask <= 0:
            return 0.0

        mid = (bid + ask) / 2.0
        if self.entry_price <= 0:
            return 0.0

        return ((mid - self.entry_price) / self.entry_price) * 100

    # =========================================================================
    # CLOSE EXECUTION
    # =========================================================================

    def _close_position(self, reason: str):
        """Submit MARKET order to close the option position."""
        if self._closing_in_progress:
            return

        self._closing_in_progress = True

        self.logger.info(
            f"📤 CLOSING | Reason={reason} | {self.trade_direction} "
            f"{self.actual_position_size}x {self.current_option_id}"
        )
        self._notify(
            f"📤 CLOSING | {reason} | {self.trade_direction} "
            f"{self.actual_position_size}x | PnL={self._get_position_pnl_pct():.1f}%"
        )

        if not self._option_instrument:
            self.logger.error("No option instrument for close — cannot close")
            self._closing_in_progress = False
            return

        qty = self._option_instrument.make_qty(self.actual_position_size)
        order = self.order_factory.market(
            instrument_id=self._option_instrument_id,
            order_side=OrderSide.SELL,
            quantity=qty,
            time_in_force=TimeInForce.DAY,
        )

        self._exit_order_id = order.client_order_id
        self._tfmith_exit_orders.add(order.client_order_id)   # private — bypasses BaseStrategy routing
        self._last_exit_reason = reason

        self.submit_order(order)

    # =========================================================================
    # ORDER FILL HANDLING
    # =========================================================================

    def on_order_filled_safe(self, event):
        """
        Handle fills — entry and exit.

        Mirrors base.py pattern: accumulate partial fills without discarding
        the tracking order ID, and only dispatch to the final handler when the
        order reaches FILLED status.
        """
        order_id = event.client_order_id
        is_entry = order_id in self._tfmith_entry_orders
        is_exit  = order_id in self._tfmith_exit_orders

        if not is_entry and not is_exit:
            return

        # Check if this is a partial fill — if so, accumulate and wait.
        # Only discard the order ID (and process final state) when FILLED.
        order = self.cache.order(order_id)
        if order and order.status == OrderStatus.PARTIALLY_FILLED:
            self._accumulate_partial_fill(event, is_entry=is_entry)
            return

        # Order is fully filled (or status unavailable) — dispatch
        if is_entry:
            self._on_entry_fill(event)
        elif is_exit:
            self._on_exit_fill(event)

    def _accumulate_partial_fill(self, event, is_entry: bool):
        """Accumulate qty and weighted-average price for a partial fill event."""
        qty = float(event.last_qty)
        px  = float(event.last_px)

        if is_entry:
            prev_qty  = self._entry_fill_qty
            prev_wavg = self._entry_fill_wavg
            new_qty   = prev_qty + qty
            self._entry_fill_wavg = (
                (prev_wavg * prev_qty + px * qty) / new_qty
            ) if new_qty > 0 else px
            self._entry_fill_qty = new_qty
            self.logger.warning(
                f"⚡ ENTRY PARTIAL FILL | +{qty} contracts | "
                f"Running total: {self._entry_fill_qty} | "
                f"Avg fill px: ${self._entry_fill_wavg:.4f}"
            )
        else:
            prev_qty  = self._exit_fill_qty
            prev_wavg = self._exit_fill_wavg
            new_qty   = prev_qty + qty
            self._exit_fill_wavg = (
                (prev_wavg * prev_qty + px * qty) / new_qty
            ) if new_qty > 0 else px
            self._exit_fill_qty = new_qty
            self.logger.warning(
                f"⚡ EXIT PARTIAL FILL | +{qty} contracts | "
                f"Running total: {self._exit_fill_qty} | "
                f"Avg fill px: ${self._exit_fill_wavg:.4f}"
            )

    def _on_entry_fill(self, event):
        """
        Capture entry price and quantity from the final fill event.

        Merges any previously accumulated partial fills with this final fill
        to produce the true total quantity and weighted-average fill price.
        """
        self._tfmith_entry_orders.discard(event.client_order_id)

        this_qty  = float(event.last_qty)
        this_px   = float(event.last_px)

        # Merge accumulated partials + this final fill
        total_qty = self._entry_fill_qty + this_qty
        if total_qty > 0:
            total_wavg = (
                (self._entry_fill_wavg * self._entry_fill_qty + this_px * this_qty)
                / total_qty
            )
        else:
            total_wavg = this_px

        self.entry_price          = total_wavg
        self._actual_qty          = total_qty
        self.actual_position_size = int(total_qty)

        # Reset entry accumulators
        self._entry_fill_qty  = 0.0
        self._entry_fill_wavg = 0.0

        # Commission on the FULL filled size
        comm = self.actual_position_size * self.commission_per_contract
        self._total_commission = comm

        self.logger.info(
            f"✅ ENTRY FILLED | {self.option_type} {self.actual_position_size}x "
            f"@ ${self.entry_price:.4f} | Comm=${comm:.2f}"
        )
        self._notify(
            f"✅ FILLED | {self.option_type} {self.actual_position_size}x "
            f"@ ${self.entry_price:.2f}"
        )

        # ── Record in DB (SPX pattern: at fill time, with actual fill data) ────────
        if self._trading_data:
            from datetime import datetime
            trade_id = f"T-{self.strategy_id[:8]}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            self._current_trade_id = trade_id
            try:
                self._trading_data.start_trade(
                    trade_id=trade_id,
                    strategy_id=self.strategy_id,
                    instrument_id=str(event.instrument_id),  # actual filled option
                    trade_type=self.option_type,             # CALL or PUT
                    entry_price=self.entry_price,            # actual fill price
                    quantity=self.actual_position_size,
                    direction="LONG",                        # TFMITH always buys
                    entry_time=self.clock.utc_now().isoformat(),
                )
                self._trading_data.record_order(
                    strategy_id=self.strategy_id,
                    instrument_id=str(event.instrument_id),
                    trade_type=self.option_type,
                    trade_direction="ENTRY",
                    order_side="BUY",
                    order_type="MARKET",
                    quantity=self.actual_position_size,
                    status="FILLED",
                    submitted_time=self.entry_time,
                    trade_id=trade_id,
                    client_order_id=str(event.client_order_id),
                    filled_time=self.clock.utc_now().isoformat(),
                    filled_quantity=self.actual_position_size,
                    filled_price=self.entry_price,
                    commission=comm,
                )
            except Exception as e:
                self.logger.error(f"Failed to record entry: {e}")

        self.save_state()

    def _on_exit_fill(self, event):
        """
        Handle exit fill — PnL, streak, allocation update, DB recording.

        Merges any previously accumulated partial exit fills with this final
        fill to produce the true total exit quantity and weighted-average price.
        """
        self._tfmith_exit_orders.discard(event.client_order_id)

        this_qty = float(event.last_qty)
        this_px  = float(event.last_px)

        # Merge accumulated partial exit fills + this final fill
        total_exit_qty = self._exit_fill_qty + this_qty
        if total_exit_qty > 0:
            total_exit_wavg = (
                (self._exit_fill_wavg * self._exit_fill_qty + this_px * this_qty)
                / total_exit_qty
            )
        else:
            total_exit_wavg = this_px

        # Reset exit accumulators
        self._exit_fill_qty  = 0.0
        self._exit_fill_wavg = 0.0

        exit_price = total_exit_wavg
        exit_qty   = total_exit_qty

        # Commission on the full exit size
        exit_comm = exit_qty * self.commission_per_contract
        total_commission = self._total_commission + exit_comm

        # ── Calculate PnL ────────────────────────────────────────────────────
        if self.entry_price is not None and self.entry_price > 0:
            raw_pnl = (exit_price - self.entry_price) * 100 * self.actual_position_size
            realized_pnl = raw_pnl - total_commission
        else:
            realized_pnl = 0.0
            raw_pnl = 0.0

        pnl_pct = ((exit_price - self.entry_price) / self.entry_price * 100) if self.entry_price and self.entry_price > 0 else 0

        # ── Update streak ────────────────────────────────────────────────────
        old_streak = self.loss_streak
        if realized_pnl < 0:
            self.loss_streak += 1
        elif realized_pnl > 0:
            self.loss_streak = 0

        # ── Update allocation ────────────────────────────────────────────────
        self.current_allocation += realized_pnl

        exit_reason = getattr(self, '_last_exit_reason', 'UNKNOWN')

        self.logger.info(
            f"{'🟢' if realized_pnl >= 0 else '🔴'} EXIT FILLED | "
            f"{self.option_type} {self.actual_position_size}x @ ${exit_price:.2f} | "
            f"RawPnL=${raw_pnl:.2f} | Comm=${total_commission:.2f} | "
            f"NetPnL=${realized_pnl:.2f} ({pnl_pct:.1f}%) | "
            f"Streak {old_streak}→{self.loss_streak} | "
            f"Alloc=${self.current_allocation:.2f} | Reason={exit_reason}"
        )
        self._notify(
            f"{'🟢' if realized_pnl >= 0 else '🔴'} CLOSED | "
            f"PnL=${realized_pnl:.2f} ({pnl_pct:.1f}%) | "
            f"Streak={self.loss_streak} | Alloc=${self.current_allocation:.2f}"
        )

        # ── Record in DB (SPX pattern) ──────────────────────────────────────
        if self._trading_data and self._current_trade_id:
            try:
                self._trading_data.record_order(
                    strategy_id=self.strategy_id,
                    instrument_id=str(event.instrument_id),
                    trade_type=self.option_type,
                    trade_direction="EXIT",
                    order_side="SELL",
                    order_type="MARKET",
                    quantity=exit_qty,
                    status="FILLED",
                    submitted_time=self.clock.utc_now().isoformat(),
                    trade_id=self._current_trade_id,
                    client_order_id=str(event.client_order_id),
                    filled_time=self.clock.utc_now().isoformat(),
                    filled_quantity=exit_qty,
                    filled_price=exit_price,
                    commission=exit_comm,
                )
                self._trading_data.close_trade(
                    trade_id=self._current_trade_id,
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    exit_time=self.clock.utc_now().isoformat(),
                    commission=total_commission,
                )
            except Exception as e:
                self.logger.error(f"Failed to record exit: {e}")

        # ── Reset position state ─────────────────────────────────────────────
        self.position_open = False
        self._closing_in_progress = False
        self.entry_price = None
        self._current_trade_id = None
        self._total_commission = 0.0
        self._actual_qty = 0.0
        self._last_position_status = {}
        # traded_today stays True — blocks re-entry

        self.save_state()

    # =========================================================================
    # STATE PERSISTENCE
    # =========================================================================

    def get_state(self) -> Dict[str, Any]:
        """Return strategy state for persistence."""
        return {
            # Persistent
            "loss_streak": self.loss_streak,
            "current_allocation": self.current_allocation,
            # Daily
            "traded_today": self.traded_today,
            "opening_price": self.opening_price,
            "change_since_open": self.change_since_open,
            "position_open": self.position_open,
            "entry_time": self.entry_time,
            "entry_price": self.entry_price,
            "option_type": self.option_type,
            "trade_direction": self.trade_direction,
            "actual_position_size": self.actual_position_size,
            "current_option_id": self.current_option_id,
            # Internal tracking
            "entry_in_progress": self.entry_in_progress,
            "_closing_in_progress": self._closing_in_progress,
            "_actual_qty": self._actual_qty,
            "_total_commission": self._total_commission,
            "current_trading_day": str(self.current_trading_day) if self.current_trading_day else None,
        }

    def set_state(self, state: Dict[str, Any]):
        """Restore strategy state."""
        # Persistent
        self.loss_streak = state.get("loss_streak", 0)
        self.current_allocation = state.get("current_allocation", self.initial_allocation)
        # Daily
        self.traded_today = state.get("traded_today", False)
        self.opening_price = state.get("opening_price")
        self.change_since_open = state.get("change_since_open", 0.0)
        self.position_open = state.get("position_open", False)
        self.entry_time = state.get("entry_time")
        self.entry_price = state.get("entry_price")
        self.option_type = state.get("option_type")
        self.trade_direction = state.get("trade_direction")
        self.actual_position_size = state.get("actual_position_size", 0)
        self.current_option_id = state.get("current_option_id")
        # Internal
        self.entry_in_progress = state.get("entry_in_progress", False)
        self._closing_in_progress = state.get("_closing_in_progress", False)
        self._actual_qty = state.get("_actual_qty", 0.0)
        self._total_commission = state.get("_total_commission", 0.0)

        if state.get("current_trading_day"):
            try:
                self.current_trading_day = date.fromisoformat(state["current_trading_day"])
            except (ValueError, TypeError):
                pass

        # Restore option instrument ID for monitoring
        if self.current_option_id:
            try:
                self._option_instrument_id = InstrumentId.from_str(self.current_option_id)
                self._option_instrument = self.cache.instrument(self._option_instrument_id)
            except Exception:
                pass

        self.logger.info(
            f"State restored | Streak={self.loss_streak} | "
            f"Alloc=${self.current_allocation:.2f} | "
            f"Traded={self.traded_today} | PosOpen={self.position_open} | "
            f"Entry={self.entry_price}"
        )

    # =========================================================================
    # UI STATUS
    # =========================================================================

    def get_custom_status(self) -> Dict[str, Any]:
        """Return position status for UI broadcasting."""
        if self.position_open and self._last_position_status:
            return self._last_position_status
        return {}

    # =========================================================================
    # STOP
    # =========================================================================

    def on_stop_safe(self):
        """Clean up when strategy stops."""
        self.logger.info(
            f"🛑 STOPPING | Traded={self.traded_today} | "
            f"PosOpen={self.position_open} | Streak={self.loss_streak}"
        )

        if self.position_open and not self._closing_in_progress:
            self._close_position("STRATEGY_STOP")

        # Unsubscribe from underlying
        if self.underlying_subscribed and self.underlying_instrument_id:
            try:
                self.unsubscribe_quote_ticks(self.underlying_instrument_id)
            except Exception:
                pass

        self.logger.info("🛑 TFMITH Strategy stopped")

    # =========================================================================
    # ABSTRACT METHOD STUBS (required by BaseStrategy)
    # =========================================================================



    def on_order_submitted_safe(self, event):
        pass

    def on_order_rejected_safe(self, event):
        """Handle order rejection."""
        order_id = event.client_order_id
        if order_id == self._entry_order_id:
            self.logger.error(f"❌ Entry order REJECTED: {event.reason}")
            self.entry_in_progress = False
            self.position_open = False
            self.traded_today = True
            self.save_state()
        elif order_id == self._exit_order_id:
            self.logger.error(f"❌ Exit order REJECTED: {event.reason}")
            self._closing_in_progress = False

    def on_order_canceled_safe(self, event):
        pass

    def on_order_expired_safe(self, event):
        pass

    def on_reset_safe(self):
        pass

    def on_resume_safe(self):
        pass

    def on_spread_ready(self, instrument):
        pass
