"""
SPX 0DTE Opening Straddle Strategy

Captures micro-volatility at market open via 0DTE SPX Straddle.
Entry at 09:30 EST (dynamic timezone handling for DST), with price-offset 
based exit monitoring and hard timeout exit.

Key Features:
- Dynamic timezone handling (America/New_York) for correct DST behavior
- Pre-filtering of 0DTE options in on_start for fast execution at market open
- Retry mechanism for graceful error handling
- Unique order tagging for leg identification
"""

from datetime import time, timedelta, datetime
from typing import Optional, List
from zoneinfo import ZoneInfo

import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import TimeEvent
from nautilus_trader.model.identifiers import InstrumentId, ClientOrderId
from nautilus_trader.model.instruments import OptionsContract
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
    price_offset : float
        Price offset for exit triggers (default: 4.0).
    timeout_seconds : int
        Hard exit timeout in seconds (default: 300 = 5 minutes).
    entry_retry_seconds : int
        How long to retry finding options at market open (default: 10).
    """

    spx_instrument_id: str = "SPX.CBOE"
    target_premium: float = 2.0
    price_offset: float = 4.0
    timeout_seconds: int = 300
    entry_retry_seconds: int = 10


class SpxOpeningStraddle(Strategy):
    """
    SPX 0DTE Opening Straddle Strategy.

    Captures micro-volatility at market open by buying a straddle (call + put)
    with 0DTE expiration. Uses price-offset triggers for individual leg exits
    and a hard timeout for safety.

    Entry Logic:
    - Pre-filters 0DTE options in on_start for fast execution
    - At 09:30:00 EST (dynamic timezone), find call and put with premium closest to target
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
        self.price_offset = config.price_offset
        self.timeout_seconds = config.timeout_seconds
        self.entry_retry_seconds = config.entry_retry_seconds

        # Pre-filtered 0DTE options (populated in on_start)
        self._prefiltered_calls: List[OptionsContract] = []
        self._prefiltered_puts: List[OptionsContract] = []
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

    def on_start(self) -> None:
        """
        Actions to be performed on strategy start.
        Pre-filter 0DTE options for fast execution at market open.
        Subscribe to SPX quote ticks for entry/exit monitoring.
        """
        self.log.info("Starting SpxOpeningStraddle strategy...")
        self.log.info(f"  SPX Instrument: {self.spx_instrument_id}")
        self.log.info(f"  Target Premium: ${self.target_premium:.2f}")
        self.log.info(f"  Price Offset: ${self.price_offset:.2f}")
        self.log.info(f"  Timeout: {self.timeout_seconds}s")
        self.log.info(f"  Entry Retry Window: {self.entry_retry_seconds}s")

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

            for inst in all_instruments:
                if not isinstance(inst, OptionsContract):
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
                    # Subscribe to quotes for this option
                    self.subscribe_quote_ticks(inst.id)
                else:
                    self._prefiltered_puts.append(inst)
                    # Subscribe to quotes for this option
                    self.subscribe_quote_ticks(inst.id)

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

    def find_best_legs(self) -> tuple[Optional[OptionsContract], Optional[OptionsContract]]:
        """
        Find the best call and put options from pre-filtered 0DTE options.
        Selects options with mid-price closest to target premium.

        Returns
        -------
        tuple[Optional[OptionsContract], Optional[OptionsContract]]
            Best call and put instruments, or (None, None) if not found.
        """
        # If pre-filtering failed, try again now
        if not self._options_prefiltered or (
            not self._prefiltered_calls and not self._prefiltered_puts
        ):
            self.log.info("Re-attempting option pre-filtering...")
            self._prefilter_0dte_options()

        if not self._prefiltered_calls:
            self.log.warning("No pre-filtered calls available")
            return None, None

        if not self._prefiltered_puts:
            self.log.warning("No pre-filtered puts available")
            return None, None

        best_call: Optional[OptionsContract] = None
        best_put: Optional[OptionsContract] = None
        min_call_diff = float('inf')
        min_put_diff = float('inf')
        calls_with_quotes = 0
        puts_with_quotes = 0

        # Find best call from pre-filtered list
        for inst in self._prefiltered_calls:
            quote = self.cache.quote_tick(inst.id)
            if not quote:
                continue
            
            calls_with_quotes += 1
            bid = float(quote.bid_price)
            ask = float(quote.ask_price)
            
            # Skip if bid/ask are invalid
            if bid <= 0 or ask <= 0:
                continue
                
            mid_price = (bid + ask) / 2.0
            diff = abs(mid_price - self.target_premium)

            if diff < min_call_diff:
                min_call_diff = diff
                best_call = inst

        # Find best put from pre-filtered list
        for inst in self._prefiltered_puts:
            quote = self.cache.quote_tick(inst.id)
            if not quote:
                continue
            
            puts_with_quotes += 1
            bid = float(quote.bid_price)
            ask = float(quote.ask_price)
            
            # Skip if bid/ask are invalid
            if bid <= 0 or ask <= 0:
                continue
                
            mid_price = (bid + ask) / 2.0
            diff = abs(mid_price - self.target_premium)

            if diff < min_put_diff:
                min_put_diff = diff
                best_put = inst

        self.log.info(
            f"Scanned {calls_with_quotes}/{len(self._prefiltered_calls)} calls, "
            f"{puts_with_quotes}/{len(self._prefiltered_puts)} puts with quotes"
        )

        if best_call:
            self.log.info(f"Best call: {best_call.id} (premium diff: ${min_call_diff:.2f})")
        if best_put:
            self.log.info(f"Best put: {best_put.id} (premium diff: ${min_put_diff:.2f})")

        return best_call, best_put

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
        positions = self.cache.positions(instrument_id=instrument_id)
        for position in positions:
            if not position.is_closed:
                self.log.info(f"Closing position for {instrument_id}")
                self.close_position(position)

    def on_event(self, event: TimeEvent) -> None:
        """
        Handle time events for the hard exit timer.
        """
        if not isinstance(event, TimeEvent):
            return

        if event.name == "hard_exit":
            self.log.warning("Hard exit timer triggered - closing all positions")
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
        # Keep pre-filtered options for reuse
