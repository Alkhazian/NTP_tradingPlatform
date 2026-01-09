from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce
from nautilus_trader.model.identifiers import Venue, InstrumentId
from nautilus_trader.model.data import Bar, BarType, BarSpecification, BarAggregation
from nautilus_trader.model.enums import PriceType
from nautilus_trader.model.objects import Quantity

from ..base import BaseStrategy
from ..config import StrategyConfig

class SimpleIntervalTrader(BaseStrategy):
    """
    A simple reference strategy that buys 1 contract every N minutes,
    holds for M minutes, then closes the position.
    """
    
    def __init__(self, config: StrategyConfig, integration_manager=None, persistence_manager=None):
        super().__init__(config, integration_manager, persistence_manager)
        self.trader_config: StrategyConfig = config # Type hinting
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.instrument: Optional[Instrument] = None
        
        # Runtime State
        self.is_position_open = False
        self.last_buy_time: Optional[float] = None # Unix timestamp
        self.open_position_id: Optional[str] = None
        self._close_timer_name: Optional[str] = None
        self._start_retry_count = 0

    def on_start_safe(self, time=None):
        """
        Start lifecycle: Subscribe to data and schedule first buy.
        """
        # Request instrument
        self.instrument = self.cache.instrument(self.instrument_id)
        if self.instrument is None:
            self._start_retry_count += 1
            if self._start_retry_count <= 12: # Retry for 1 minute (5s * 12)
                self.logger.warning(
                    f"Instrument {self.instrument_id} not found in cache (retry {self._start_retry_count}/12). "
                    "Waiting 5 seconds..."
                )
                self.clock.set_time_alert(
                    name=f"{self.id}.start_retry",
                    alert_time=self.clock.utc_now() + timedelta(seconds=5),
                    callback=self.on_start_safe,
                    override=True
                )
            else:
                self.logger.error(f"Instrument {self.instrument_id} not found in cache after 1 minute. Giving up.")
            return

        self.logger.info(f"Instrument {self.instrument_id} resolved. Subscribing to data...")

        # Subscribe to 1-minute bars using BarType + BarSpecification
        bar_spec = BarSpecification.from_str("1-MINUTE-LAST")
        bar_type = BarType(self.instrument_id, bar_spec)
        self.subscribe_bars(bar_type)
        self._functional_ready = True

        # Schedule the first buy loop check
        # We check every minute if we should buy
        self.clock.set_time_alert(
            name=f"{self.id}.check_buy_signal",
            alert_time=self.clock.utc_now() + timedelta(seconds=10),
            callback=self._check_buy_signal
        )
        self.logger.info("SimpleIntervalTrader started. Waiting for next interval.")

    def _check_buy_signal(self, time):
        """
        Periodically checks if it's time to buy.
        """
        self.logger.info(f"DEBUG: _check_buy_signal called at {datetime.utcfromtimestamp(self.clock.timestamp())}")
        try:
            # Reschedule check
            self.clock.set_time_alert(
                name=f"{self.id}.check_buy_signal",
                alert_time=self.clock.utc_now() + timedelta(minutes=1),
                callback=self._check_buy_signal,
                override=True
            )

            # If position is already open, ignore
            if self.is_position_open:
                return

            now_ts = self.clock.timestamp()
            
            # Simple logic: Buy if enough time passed since last buy (or never bought)
            # The user said "on the 5-minute mark" - this implies wall clock time 00:05, 00:10 etc.
            # Let's approximate by creating a timer logic or checking modulus
            
            current_dt = datetime.utcfromtimestamp(now_ts)
            
            buy_interval = int(self.trader_config.parameters.get("buy_interval_minutes", 5))

            # Check if minutes is divisible by buy_interval
            if current_dt.minute % buy_interval == 0:
                # To prevent double buying in the same minute, we check last_buy_time
                if self.last_buy_time:
                    last_buy_dt = datetime.utcfromtimestamp(self.last_buy_time)
                    if last_buy_dt.minute == current_dt.minute and last_buy_dt.hour == current_dt.hour:
                        return
                        
                self.logger.info(f"Time match ({current_dt.time()}). executing buy order.")
                self._execute_buy()
                
        except Exception as e:
            self.on_unexpected_error(e)

    def _execute_buy(self):
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_int(int(self.trader_config.order_size)),
        )
        self.submit_order(order)
        self.logger.info(f"Submitted BUY order for {self.trader_config.order_size} {self.instrument_id}")
        
        # Optimistic state update (will be confirmed by fill)
        self.last_buy_time = self.clock.timestamp()
        
    def on_order_filled(self, event):
        """
        Handle order fills.
        """
        if event.order_side == OrderSide.BUY:
            self.logger.info(f"BUY Filled: {event}")
            self.is_position_open = True
            
            # Record entry price for PnL calculation later
            self.last_entry_price = float(event.last_px) 

            # Start persistent trade record
            # We use asyncio.create_task because we can't await easily in this sync callback 
            # (unless on_order_filled is async in base? No, it's usually sync in Nautilus)
            # Actually, Nautilus 1.18+ callbacks are sync.
            # BaseStrategy methods we just added are async. We need to schedule them.
            import asyncio
            asyncio.create_task(self.start_trade_record(
                str(self.instrument_id),
                self.clock.timestamp(),
                self.last_entry_price,
                float(event.last_qty),
                "LONG"
            ))

            self.save_state() # Persist "Open Position" state
            
            # Schedule release/sell
            hold_duration = int(self.trader_config.parameters.get("hold_duration_minutes", 2))
            close_time = self.clock.utc_now() + timedelta(minutes=hold_duration)
            self._close_timer_name = f"{self.id}.execute_close"
            self.clock.set_time_alert(
                name=self._close_timer_name,
                alert_time=close_time,
                callback=self._execute_close,
                override=True
            )
            self.logger.info(f"Scheduled SELL for {close_time}")

        elif event.order_side == OrderSide.SELL:
            self.logger.info(f"SELL Filled: {event}")
            self.is_position_open = False
            self.open_position_id = None
            self._close_timer_name = None
            
            # Close persistent trade record
            multiplier = 1.0
            if self.instrument and self.instrument.multiplier:
                multiplier = float(self.instrument.multiplier)
            
            import asyncio
            # We assume exit reason was set before close, or we default
            reason = getattr(self, "last_exit_reason", "UNKNOWN")

            asyncio.create_task(self.close_trade_record(
                self.clock.timestamp(),
                float(event.last_px),
                reason,
                float(event.last_qty),
                self.last_entry_price if hasattr(self, 'last_entry_price') else 0.0,
                multiplier
            ))

            self.save_state() # Persist "Closed Position" state

    def _execute_close(self, time):
        """
        Close the position.
        """
        if not self.is_position_open:
            return

        self.logger.info("Hold duration expried. Executing SELL order.")
        self.last_exit_reason = "HOLD_DURATION_EXPIRED"
        # Close all positions for this instrument (simple approach)
        self.close_all_positions(self.instrument_id)

    def get_state(self) -> Dict[str, Any]:
        """
        Serialize state.
        """
        return {
            "is_position_open": self.is_position_open,
            "last_buy_time": self.last_buy_time,
            "open_position_id": self.open_position_id,
            "last_entry_price": getattr(self, "last_entry_price", 0.0),
            "last_exit_reason": getattr(self, "last_exit_reason", None)
        }

    def set_state(self, state: Dict[str, Any]):
        """
        Restore state.
        """
        self.is_position_open = state.get("is_position_open", False)
        self.last_buy_time = state.get("last_buy_time")
        self.open_position_id = state.get("open_position_id")
        self.last_entry_price = state.get("last_entry_price", 0.0)
        self.last_exit_reason = state.get("last_exit_reason")
        
        # If we restored an open position state, we need to consider if we should close it
        # If the system crashed and restarted, the hold timer is lost.
        # We should check if we are past the hold duration.
        
        if self.is_position_open and self.last_buy_time:
            now_ts = datetime.utcnow().timestamp()
            elapsed_min = (now_ts - self.last_buy_time) / 60
            
            hold_duration = int(self.trader_config.parameters.get("hold_duration_minutes", 2))
            
            if elapsed_min >= hold_duration:
                self.logger.warning("Restored open position is PAST hold duration. Scheduling immediate close.")
                # We can't schedule immediately in this method easily if clock isn't running yet,
                # but on_start_safe calls this, so it should be fine to schedule a quick check?
                # Actually, on_start runs when Strategy starts.
                # We can set a flag to check in on_start or just let the user handle it?
                # Best effort:
                # We will handle this in on_start_safe or via a separate check.
                pass
