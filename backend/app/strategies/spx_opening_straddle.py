"""
SPX 0DTE Opening Straddle Strategy

Captures micro-volatility at market open via 0DTE SPX Straddle.
Entry at 09:30 EST (dynamic timezone handling for DST), with price-offset 
based exit monitoring and hard timeout exit.

Key Features:
- Dynamic timezone handling (America/New_York) for correct DST behavior
- Pre-filtering of 0DTE options in on_start for fast execution at market open
- Balanced leg selection with max_premium_deviation constraint
- Retry mechanism for graceful error handling
- Unique order tagging for leg identification
- Test mode for dry run validation without real trades
- Subscription optimization post-entry
"""

from datetime import time, timedelta, datetime
from typing import Optional, List, Dict, Any, Callable
from zoneinfo import ZoneInfo
import json

import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, ClientOrderId
from nautilus_trader.model.instruments import OptionContract
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy


# Timezone for market open detection
NY_TZ = ZoneInfo("America/New_York")


class SpxOpeningConfig(StrategyConfig, frozen=True):
    """
    Configuration for the SPX Opening Straddle strategy.

    Parameters
    ----------
    spx_instrument_id : str
        The SPX underlying instrument ID (e.g., "SPX.CBOE").
    target_premium : float
        Target premium for option selection (default: 2.0).
    max_premium_deviation : float
        Max deviation from target for balanced straddle (default: 0.50).
    price_offset : float
        Price offset for exit triggers (default: 4.0).
    timeout_seconds : int
        Hard exit timeout in seconds (default: 300 = 5 minutes).
    entry_retry_seconds : int
        How long to retry finding options at market open (default: 10).
    test_mode : bool
        If True, run in dry-run mode without real trades (default: False).
    """

    spx_instrument_id: str = "SPX.CBOE"
    target_premium: float = 2.0
    max_premium_deviation: float = 0.50
    price_offset: float = 4.0
    timeout_seconds: int = 300
    entry_retry_seconds: int = 10
    test_mode: bool = False


class SpxOpeningStraddle(Strategy):
    """
    SPX 0DTE Opening Straddle Strategy.

    Captures micro-volatility at market open by buying a straddle (call + put)
    with 0DTE expiration. Uses price-offset triggers for individual leg exits
    and a hard timeout for safety.

    Entry Logic:
    - Pre-filters 0DTE options in on_start for fast execution
    - At 09:30:00 EST (dynamic timezone), find call and put with premium closest to target
    - Both legs must be within target_premium +/- max_premium_deviation
    - Buy both legs with market orders
    - Retry for entry_retry_seconds if options not immediately available

    Exit Logic:
    - If underlying price rises by price_offset, close call leg
    - If underlying price falls by price_offset, close put leg  
    - Hard exit after timeout_seconds regardless of P&L
    """

    def __init__(self, config: SpxOpeningConfig) -> None:
        super().__init__(config)

        # Configuration
        self.spx_instrument_id = InstrumentId.from_str(config.spx_instrument_id)
        self.target_premium = config.target_premium
        self.max_premium_deviation = config.max_premium_deviation
        self.price_offset = config.price_offset
        self.timeout_seconds = config.timeout_seconds
        self.entry_retry_seconds = config.entry_retry_seconds
        self.test_mode = config.test_mode

        # Pre-filtered 0DTE options (populated in on_start)
        self._prefiltered_calls: List[OptionContract] = []
        self._prefiltered_puts: List[OptionContract] = []
        self._subscribed_option_ids: List[InstrumentId] = []
        self._options_prefiltered: bool = False

        # State tracking
        self.positions_opened: bool = False
        self.entry_underlying_price: Optional[float] = None
        self.call_instrument_id: Optional[InstrumentId] = None
        self.put_instrument_id: Optional[InstrumentId] = None
        self.call_closed: bool = False
        self.put_closed: bool = False
        
        # Entry timing
        self._entry_window_start: Optional[datetime] = None
        self._entry_attempted: bool = False
        
        # Order tracking for leg identification
        self._call_client_order_id: Optional[ClientOrderId] = None
        self._put_client_order_id: Optional[ClientOrderId] = None
        
        # Test mode logs (for dry run)
        self._test_logs: List[Dict[str, Any]] = []
        self._log_callback: Optional[Callable[[Dict], None]] = None

    def set_log_callback(self, callback: Callable[[Dict], None]) -> None:
        """Set external callback for log broadcasting."""
        self._log_callback = callback

    def _broadcast_log(self, step: str, message: str, data: Optional[Dict] = None, level: str = "info") -> None:
        """Broadcast structured log for UI consumption."""
        log_entry = {
            "timestamp": self.clock.utc_now().isoformat(),
            "step": step,
            "message": message,
            "data": data or {},
            "level": level,
        }
        self._test_logs.append(log_entry)
        
        # Also use strategy logger
        if level == "error":
            self.log.error(f"[{step}] {message}")
        elif level == "warning":
            self.log.warning(f"[{step}] {message}")
        else:
            self.log.info(f"[{step}] {message}")
        
        # Call external callback if set
        if self._log_callback:
            try:
                self._log_callback(log_entry)
            except Exception:
                pass

    def get_test_logs(self) -> List[Dict[str, Any]]:
        """Get all test logs for UI display."""
        return self._test_logs.copy()

    def clear_test_logs(self) -> None:
        """Clear test logs buffer."""
        self._test_logs.clear()

    def _get_ny_time(self) -> time:
        """Get current time in America/New_York timezone."""
        utc_now = self.clock.utc_now()
        ny_now = utc_now.astimezone(NY_TZ)
        return ny_now.time()

    def _get_ny_datetime(self) -> datetime:
        """Get current datetime in America/New_York timezone."""
        utc_now = self.clock.utc_now()
        return utc_now.astimezone(NY_TZ)

    def _is_market_open_window(self) -> bool:
        """
        Check if current time is in the market open window (09:30:00 - 09:30:XX EST).
        Uses dynamic timezone conversion to handle DST correctly.
        """
        ny_time = self._get_ny_time()
        market_open = time(9, 30, 0)
        market_open_window_end = time(9, 30, self.entry_retry_seconds)
        return market_open <= ny_time <= market_open_window_end

    def run_health_test(self) -> List[Dict[str, Any]]:
        """
        Run a comprehensive health test (dry run) of the strategy.
        Tests all components without executing real trades.
        
        Returns list of structured log entries for UI display.
        """
        self.clear_test_logs()
        
        # Step 1: CONNECTION_CHECK
        self._broadcast_log(
            step="CONNECTION_CHECK",
            message="Verifying Trading Node and Data Provider connectivity...",
            level="info"
        )
        
        try:
            cache_available = self.cache is not None
            clock_available = self.clock is not None
            self._broadcast_log(
                step="CONNECTION_CHECK",
                message=f"Trading Node connected: cache={'✓' if cache_available else '✗'}, clock={'✓' if clock_available else '✗'}",
                data={"cache": cache_available, "clock": clock_available},
                level="success" if cache_available and clock_available else "error"
            )
        except Exception as e:
            self._broadcast_log(
                step="CONNECTION_CHECK",
                message=f"Connection check failed: {e}",
                level="error"
            )
            return self.get_test_logs()

        # Step 2: DATA_SUBSCRIPTION
        self._broadcast_log(
            step="DATA_SUBSCRIPTION",
            message=f"Checking subscription for SPX Index: {self.spx_instrument_id}",
            level="info"
        )
        
        spx_quote = self.cache.quote_tick(self.spx_instrument_id)
        if spx_quote:
            bid = float(spx_quote.bid_price)
            ask = float(spx_quote.ask_price)
            mid = (bid + ask) / 2
            self._broadcast_log(
                step="DATA_SUBSCRIPTION",
                message=f"SPX quote available: Bid=${bid:.2f}, Ask=${ask:.2f}, Mid=${mid:.2f}",
                data={"bid": bid, "ask": ask, "mid": mid},
                level="success"
            )
        else:
            self._broadcast_log(
                step="DATA_SUBSCRIPTION",
                message="No SPX quote available - ensure data subscription is active",
                level="warning"
            )

        # Step 3: CHAIN_SCAN
        self._broadcast_log(
            step="CHAIN_SCAN",
            message="Scanning for 0DTE SPX options...",
            level="info"
        )
        
        # Force re-filter to get current state
        self._prefilter_0dte_options()
        
        sample_calls = self._prefiltered_calls[:5]
        sample_puts = self._prefiltered_puts[:5]
        
        call_samples = [str(c.id) for c in sample_calls]
        put_samples = [str(p.id) for p in sample_puts]
        
        self._broadcast_log(
            step="CHAIN_SCAN",
            message=f"Found {len(self._prefiltered_calls)} calls, {len(self._prefiltered_puts)} puts for today",
            data={
                "total_calls": len(self._prefiltered_calls),
                "total_puts": len(self._prefiltered_puts),
                "sample_calls": call_samples,
                "sample_puts": put_samples,
            },
            level="success" if self._prefiltered_calls and self._prefiltered_puts else "warning"
        )

        # Step 4: MOCK_EXECUTION
        self._broadcast_log(
            step="MOCK_EXECUTION",
            message=f"Simulating 09:30:00 EST trigger (current NY time: {self._get_ny_time()})",
            level="info"
        )
        
        # Find best legs as we would at market open
        call_inst, put_inst, call_mid, put_mid = self._find_best_legs_with_prices()
        
        if call_inst and put_inst:
            self._broadcast_log(
                step="MOCK_EXECUTION",
                message=f"Selected legs - Call: {call_inst.id} @ ${call_mid:.2f}, Put: {put_inst.id} @ ${put_mid:.2f}",
                data={
                    "call_id": str(call_inst.id),
                    "call_mid": call_mid,
                    "put_id": str(put_inst.id),
                    "put_mid": put_mid,
                    "target_premium": self.target_premium,
                },
                level="success"
            )
            
            # Check balanced straddle constraint
            is_balanced = self._validate_balanced_legs(call_mid, put_mid)
            self._broadcast_log(
                step="MOCK_EXECUTION",
                message=f"Balanced straddle check: {'PASS ✓' if is_balanced else 'FAIL ✗'} (range: ${self.target_premium - self.max_premium_deviation:.2f} - ${self.target_premium + self.max_premium_deviation:.2f})",
                data={"is_balanced": is_balanced, "min": self.target_premium - self.max_premium_deviation, "max": self.target_premium + self.max_premium_deviation},
                level="success" if is_balanced else "warning"
            )
        else:
            self._broadcast_log(
                step="MOCK_EXECUTION",
                message="Could not find suitable call and/or put legs",
                level="error"
            )
            return self.get_test_logs()

        # Step 5: ORDER_SIMULATION
        mock_underlying_price = float(spx_quote.ask_price) if spx_quote else 5900.0
        
        self._broadcast_log(
            step="ORDER_SIMULATION",
            message="Simulating order creation (NOT sending to exchange)",
            level="info"
        )
        
        self._broadcast_log(
            step="ORDER_SIMULATION",
            message=f"CALL ORDER: BUY 1x {call_inst.id} @ MARKET (mid: ${call_mid:.2f})",
            data={"side": "BUY", "quantity": 1, "instrument": str(call_inst.id), "mid_price": call_mid, "order_type": "MARKET"},
            level="info"
        )
        
        self._broadcast_log(
            step="ORDER_SIMULATION",
            message=f"PUT ORDER: BUY 1x {put_inst.id} @ MARKET (mid: ${put_mid:.2f})",
            data={"side": "BUY", "quantity": 1, "instrument": str(put_inst.id), "mid_price": put_mid, "order_type": "MARKET"},
            level="info"
        )

        # Step 6: EXIT_SIMULATION
        self._broadcast_log(
            step="EXIT_SIMULATION",
            message=f"Calculating exit targets based on mock entry at SPX ${mock_underlying_price:.2f}",
            level="info"
        )
        
        call_exit_price = mock_underlying_price + self.price_offset
        put_exit_price = mock_underlying_price - self.price_offset
        
        self._broadcast_log(
            step="EXIT_SIMULATION",
            message=f"Exit targets - Call exit: SPX >= ${call_exit_price:.2f} (+{self.price_offset}), Put exit: SPX <= ${put_exit_price:.2f} (-{self.price_offset})",
            data={
                "entry_price": mock_underlying_price,
                "call_exit_trigger": call_exit_price,
                "put_exit_trigger": put_exit_price,
                "price_offset": self.price_offset,
            },
            level="success"
        )
        
        self._broadcast_log(
            step="EXIT_SIMULATION",
            message=f"Hard exit timer would trigger in {self.timeout_seconds} seconds ({self.timeout_seconds/60:.1f} minutes)",
            data={"timeout_seconds": self.timeout_seconds},
            level="info"
        )

        self._broadcast_log(
            step="TEST_COMPLETE",
            message="Health test completed successfully ✓",
            level="success"
        )
        
        return self.get_test_logs()

    def _validate_balanced_legs(self, call_mid: float, put_mid: float) -> bool:
        """
        Validate that both legs are within the premium deviation range.
        
        Returns True if both legs meet the balanced straddle criteria.
        """
        min_premium = self.target_premium - self.max_premium_deviation
        max_premium = self.target_premium + self.max_premium_deviation
        
        call_valid = min_premium <= call_mid <= max_premium
        put_valid = min_premium <= put_mid <= max_premium
        
        return call_valid and put_valid

    def on_start(self) -> None:
        """
        Actions to be performed on strategy start.
        Pre-filter 0DTE options for fast execution at market open.
        Subscribe to SPX quote ticks for entry/exit monitoring.
        """
        self.log.info("Starting SpxOpeningStraddle strategy...")
        self.log.info(f"  SPX Instrument: {self.spx_instrument_id}")
        self.log.info(f"  Target Premium: ${self.target_premium:.2f}")
        self.log.info(f"  Max Premium Deviation: ${self.max_premium_deviation:.2f}")
        self.log.info(f"  Price Offset: ${self.price_offset:.2f}")
        self.log.info(f"  Timeout: {self.timeout_seconds}s")
        self.log.info(f"  Entry Retry Window: {self.entry_retry_seconds}s")
        self.log.info(f"  Test Mode: {self.test_mode}")

        # Subscribe to SPX underlying quote ticks
        self.subscribe_quote_ticks(self.spx_instrument_id)
        self.log.info(f"Subscribed to quote ticks for {self.spx_instrument_id}")

        # Pre-filter 0DTE options for fast execution at market open
        self._prefilter_0dte_options()

    def _prefilter_0dte_options(self) -> None:
        """
        Pre-filter the instrument cache for 0DTE SPX options.
        This runs at strategy start so we don't scan all instruments at market open.
        """
        try:
            all_instruments = self.cache.instruments()
            if not all_instruments:
                self.log.warning("No instruments found in cache during pre-filtering")
                return

            # Get today's date in NY timezone
            ny_now = self._get_ny_datetime()
            today = ny_now.date()

            self._prefiltered_calls = []
            self._prefiltered_puts = []
            self._subscribed_option_ids = []

            for inst in all_instruments:
                if not isinstance(inst, OptionContract):
                    continue
                    
                # Check if this is an SPX option
                if "SPX" not in str(inst.id.symbol):
                    continue
                    
                # Check if it's 0DTE (expires today)
                if hasattr(inst, 'expiration_date'):
                    exp_date = inst.expiration_date
                    if hasattr(exp_date, 'date'):
                        exp_date = exp_date.date()
                    if exp_date != today:
                        continue
                else:
                    continue

                # Categorize as call or put
                is_call = False
                if hasattr(inst, 'option_kind'):
                    is_call = str(inst.option_kind).upper() == "CALL"
                elif hasattr(inst, 'is_call'):
                    is_call = inst.is_call

                if is_call:
                    self._prefiltered_calls.append(inst)
                else:
                    self._prefiltered_puts.append(inst)
                    
                # Subscribe to quotes for this option
                self.subscribe_quote_ticks(inst.id)
                self._subscribed_option_ids.append(inst.id)

            self._options_prefiltered = True
            self.log.info(
                f"Pre-filtered 0DTE options: {len(self._prefiltered_calls)} calls, "
                f"{len(self._prefiltered_puts)} puts for {today}"
            )

            if not self._prefiltered_calls or not self._prefiltered_puts:
                self.log.warning(
                    "No 0DTE options found during pre-filtering. "
                    "Will retry at market open."
                )

        except Exception as e:
            self.log.error(f"Error pre-filtering options: {e}")
            self._options_prefiltered = False

    def _unsubscribe_option_chain(self) -> None:
        """
        Unsubscribe from the broad option chain after entry.
        Keeps only SPX index and the two held contract subscriptions.
        """
        try:
            unsubscribed_count = 0
            for inst_id in self._subscribed_option_ids:
                # Keep subscriptions for our held positions
                if inst_id == self.call_instrument_id or inst_id == self.put_instrument_id:
                    continue
                    
                try:
                    self.unsubscribe_quote_ticks(inst_id)
                    unsubscribed_count += 1
                except Exception:
                    pass  # Some may not be subscribed
                    
            self.log.info(
                f"Unsubscribed from {unsubscribed_count} option chain instruments. "
                f"Keeping: SPX, {self.call_instrument_id}, {self.put_instrument_id}"
            )
            
            # Clear the list but keep our held instruments
            self._subscribed_option_ids = [
                self.call_instrument_id,
                self.put_instrument_id,
            ]
            
        except Exception as e:
            self.log.warning(f"Error during option chain cleanup: {e}")

    def _find_best_legs_with_prices(self) -> tuple[Optional[OptionContract], Optional[OptionContract], float, float]:
        """
        Find best call and put with their mid prices.
        Returns (call_inst, put_inst, call_mid, put_mid).
        """
        if not self._options_prefiltered or (
            not self._prefiltered_calls and not self._prefiltered_puts
        ):
            self._prefilter_0dte_options()

        best_call: Optional[OptionContract] = None
        best_put: Optional[OptionContract] = None
        best_call_mid = 0.0
        best_put_mid = 0.0
        min_call_diff = float('inf')
        min_put_diff = float('inf')

        # Find best call
        for inst in self._prefiltered_calls:
            quote = self.cache.quote_tick(inst.id)
            if not quote:
                continue
            
            bid = float(quote.bid_price)
            ask = float(quote.ask_price)
            if bid <= 0 or ask <= 0:
                continue
                
            mid_price = (bid + ask) / 2.0
            diff = abs(mid_price - self.target_premium)

            if diff < min_call_diff:
                min_call_diff = diff
                best_call = inst
                best_call_mid = mid_price

        # Find best put
        for inst in self._prefiltered_puts:
            quote = self.cache.quote_tick(inst.id)
            if not quote:
                continue
            
            bid = float(quote.bid_price)
            ask = float(quote.ask_price)
            if bid <= 0 or ask <= 0:
                continue
                
            mid_price = (bid + ask) / 2.0
            diff = abs(mid_price - self.target_premium)

            if diff < min_put_diff:
                min_put_diff = diff
                best_put = inst
                best_put_mid = mid_price

        return best_call, best_put, best_call_mid, best_put_mid

    def find_best_legs(self) -> tuple[Optional[OptionContract], Optional[OptionContract]]:
        """
        Find the best call and put options from pre-filtered 0DTE options.
        Validates balanced straddle criteria.

        Returns
        -------
        tuple[Optional[OptionContract], Optional[OptionContract]]
            Best call and put instruments, or (None, None) if not found or unbalanced.
        """
        call_inst, put_inst, call_mid, put_mid = self._find_best_legs_with_prices()

        if not call_inst:
            self.log.warning("No suitable call found in pre-filtered options")
            return None, None

        if not put_inst:
            self.log.warning("No suitable put found in pre-filtered options")
            return None, None

        # Validate balanced straddle
        if not self._validate_balanced_legs(call_mid, put_mid):
            min_p = self.target_premium - self.max_premium_deviation
            max_p = self.target_premium + self.max_premium_deviation
            self.log.warning(
                f"Balanced straddle validation FAILED - "
                f"Call mid: ${call_mid:.2f}, Put mid: ${put_mid:.2f}, "
                f"Required range: ${min_p:.2f} - ${max_p:.2f}"
            )
            return None, None

        self.log.info(f"Best call: {call_inst.id} @ ${call_mid:.2f}")
        self.log.info(f"Best put: {put_inst.id} @ ${put_mid:.2f}")
        self.log.info("Balanced straddle validation PASSED ✓")

        return call_inst, put_inst

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """
        Handle incoming quote ticks.

        At 09:30 EST (dynamic timezone), open the straddle position.
        After positions are opened, monitor for exit conditions.
        """
        # Only process SPX underlying quotes for entry/exit logic
        if tick.instrument_id != self.spx_instrument_id:
            return

        # Calculate mid price of underlying
        bid = float(tick.bid_price)
        ask = float(tick.ask_price)
        current_price = (bid + ask) / 2.0

        # 1. Opening Trigger - Check if it's within market open window (09:30 EST)
        if not self.positions_opened and not self._entry_attempted:
            if self._is_market_open_window():
                self.log.info(
                    f"Market open window detected at {self._get_ny_time()} EST - "
                    "initiating straddle entry"
                )
                self._execute_entry(current_price)
                return

        # 2. Exit Monitoring (Independent Legs)
        if self.positions_opened and self.entry_underlying_price is not None:
            # Broadcast distance to exit for UI
            distance_to_call_exit = (self.entry_underlying_price + self.price_offset) - current_price
            distance_to_put_exit = current_price - (self.entry_underlying_price - self.price_offset)
            
            # Check call exit condition: underlying rose by price_offset
            if not self.call_closed and self.call_instrument_id:
                if current_price >= self.entry_underlying_price + self.price_offset:
                    self.log.info(
                        f"Call exit triggered: price {current_price:.2f} >= "
                        f"entry {self.entry_underlying_price:.2f} + offset {self.price_offset}"
                    )
                    self._close_position_for_instrument(self.call_instrument_id)
                    self.call_closed = True

            # Check put exit condition: underlying fell by price_offset
            if not self.put_closed and self.put_instrument_id:
                if current_price <= self.entry_underlying_price - self.price_offset:
                    self.log.info(
                        f"Put exit triggered: price {current_price:.2f} <= "
                        f"entry {self.entry_underlying_price:.2f} - offset {self.price_offset}"
                    )
                    self._close_position_for_instrument(self.put_instrument_id)
                    self.put_closed = True

    def get_strategy_status(self) -> Dict[str, Any]:
        """Get current strategy status for UI display."""
        status = {
            "positions_opened": self.positions_opened,
            "entry_underlying_price": self.entry_underlying_price,
            "call_instrument_id": str(self.call_instrument_id) if self.call_instrument_id else None,
            "put_instrument_id": str(self.put_instrument_id) if self.put_instrument_id else None,
            "call_closed": self.call_closed,
            "put_closed": self.put_closed,
            "call_exit_target": None,
            "put_exit_target": None,
            "distance_to_call_exit": None,
            "distance_to_put_exit": None,
        }
        
        if self.entry_underlying_price:
            status["call_exit_target"] = self.entry_underlying_price + self.price_offset
            status["put_exit_target"] = self.entry_underlying_price - self.price_offset
            
            # Get current price for distance calculation
            spx_quote = self.cache.quote_tick(self.spx_instrument_id)
            if spx_quote:
                current = (float(spx_quote.bid_price) + float(spx_quote.ask_price)) / 2
                status["current_spx_price"] = current
                status["distance_to_call_exit"] = status["call_exit_target"] - current
                status["distance_to_put_exit"] = current - status["put_exit_target"]
        
        return status

    def _execute_entry(self, underlying_price: float) -> None:
        """
        Execute the straddle entry by finding best legs and submitting orders.
        Includes retry mechanism for graceful error handling.
        """
        # Capture entry price from the first quote at market open
        self.entry_underlying_price = underlying_price
        self.log.info(f"Entry underlying price captured: ${underlying_price:.2f}")

        call_inst, put_inst = self.find_best_legs()

        if not call_inst or not put_inst:
            # Check if we're still within retry window
            ny_time = self._get_ny_time()
            retry_end = time(9, 30, self.entry_retry_seconds)
            
            if ny_time <= retry_end:
                self.log.warning(
                    f"Could not find suitable legs at {ny_time} EST. "
                    f"Retrying until 09:30:{self.entry_retry_seconds}..."
                )
                return  # Will retry on next quote tick
            else:
                self.log.error(
                    "Could not find suitable call and put legs after retry window - aborting entry"
                )
                self._entry_attempted = True
                return

        self._entry_attempted = True
        self.call_instrument_id = call_inst.id
        self.put_instrument_id = put_inst.id

        # In test mode, don't submit real orders
        if self.test_mode:
            self.log.info("TEST MODE - Simulating order submission (no real trades)")
            self.positions_opened = True
            self._unsubscribe_option_chain()
            return

        # Generate unique client order IDs for leg identification
        timestamp_tag = str(self.clock.timestamp_ns())[-8:]
        
        # Submit market order for call leg
        self._call_client_order_id = ClientOrderId(f"SPX_STRADDLE_CALL_{timestamp_tag}")
        call_order = self.order_factory.market(
            instrument_id=call_inst.id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_int(1),
            time_in_force=TimeInForce.IOC,
            client_order_id=self._call_client_order_id,
        )
        self.submit_order(call_order)
        self.log.info(f"Submitted BUY order for call: {call_inst.id} (ID: {self._call_client_order_id})")

        # Submit market order for put leg
        self._put_client_order_id = ClientOrderId(f"SPX_STRADDLE_PUT_{timestamp_tag}")
        put_order = self.order_factory.market(
            instrument_id=put_inst.id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_int(1),
            time_in_force=TimeInForce.IOC,
            client_order_id=self._put_client_order_id,
        )
        self.submit_order(put_order)
        self.log.info(f"Submitted BUY order for put: {put_inst.id} (ID: {self._put_client_order_id})")

        self.positions_opened = True

        # Unsubscribe from broad option chain to save bandwidth
        self._unsubscribe_option_chain()

        # Set hard exit timer using pandas Timedelta for NautilusTrader compatibility
        exit_time = self.clock.utc_now() + pd.Timedelta(seconds=self.timeout_seconds)
        self.clock.set_time_alert(
            name="hard_exit",
            alert_time=exit_time,
        )
        self.log.info(f"Hard exit timer set for {exit_time}")

    def _close_position_for_instrument(self, instrument_id: InstrumentId) -> None:
        """
        Close any open position for the given instrument.
        """
        if self.test_mode:
            self.log.info(f"TEST MODE - Simulating position close for {instrument_id}")
            return
            
        positions = self.cache.positions(instrument_id=instrument_id)
        for position in positions:
            if not position.is_closed:
                self.log.info(f"Closing position for {instrument_id}")
                self.close_position(position)

    def on_event(self, event) -> None:
        """
        Handle time events for the hard exit timer.
        """
        # Check if this is a time alert event
        if not hasattr(event, 'name'):
            return

        if event.name == "hard_exit":
            self.log.warning("Hard exit timer triggered - closing all positions")
            if not self.test_mode:
                self.close_all_positions()
            self.call_closed = True
            self.put_closed = True

    def on_stop(self) -> None:
        """
        Actions to be performed on strategy stop.
        Cancel any pending timers and close positions.
        """
        self.log.info("Stopping SpxOpeningStraddle strategy")
        
        # Cancel the hard exit timer if still active
        try:
            self.clock.cancel_timer("hard_exit")
        except Exception:
            pass  # Timer may not exist

        # Close any remaining positions
        if not self.test_mode:
            self.close_all_positions()

    def on_reset(self) -> None:
        """
        Actions to be performed on strategy reset.
        Reset all state variables.
        """
        self.positions_opened = False
        self.entry_underlying_price = None
        self.call_instrument_id = None
        self.put_instrument_id = None
        self.call_closed = False
        self.put_closed = False
        self._entry_attempted = False
        self._entry_window_start = None
        self._call_client_order_id = None
        self._put_client_order_id = None
        self._test_logs.clear()
        # Keep pre-filtered options for reuse
