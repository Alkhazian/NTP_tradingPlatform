"""
Phase 1: Simplified Interval Trader
- Only contains strategy logic (entry/exit signals)
- Uses base class for all operational concerns
- Bar-driven instead of timer-driven
- No duplicate order logic (handled by base)
"""

from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Quantity
from ..base import BaseStrategy
from ..config import StrategyConfig


class SimpleIntervalTrader(BaseStrategy):
    """
    Simple strategy that:
    1. Buys 1 contract on specified time intervals (e.g., every 5 minutes)
    2. Holds for specified duration (e.g., 2 minutes)
    3. Closes position when hold duration expires
    
    STRATEGY LOGIC ONLY - operational concerns handled by BaseStrategy.
    """
    
    def __init__(
        self, 
        config: StrategyConfig, 
        integration_manager=None, 
        persistence_manager=None
    ):
        super().__init__(config, integration_manager, persistence_manager)
        
        # Strategy parameters
        self.buy_interval_minutes = int(
            self.strategy_config.parameters.get("buy_interval_minutes", 5)
        )
        self.hold_duration_minutes = int(
            self.strategy_config.parameters.get("hold_duration_minutes", 2)
        )
        
        # Strategy state
        self.last_buy_time: Optional[datetime] = None
        self._close_timer_name: Optional[str] = None
        self._had_scheduled_close = False

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    def on_start_safe(self):
        """
        Called by base after instrument is ready.
        Subscribe to bar data.
        """
        # Subscribe to 1-minute bars
        from nautilus_trader.model.data import BarSpecification
        
        bar_type = BarType(
            self.instrument_id,
            BarSpecification.from_str("1-MINUTE-MID")
        )
        self.subscribe_bars(bar_type)
        
        self.logger.info(
            f"SimpleIntervalTrader started: "
            f"buy_interval={self.buy_interval_minutes}m, "
            f"hold_duration={self.hold_duration_minutes}m"
        )
        
        # Check for any positions that need cleanup or rescheduling
        self._recover_state_logic()

    def on_stop_safe(self):
        """Cleanup timers."""
        self.logger.info("SimpleIntervalTrader stopping...")
        if self._close_timer_name:
            try:
                self.clock.cancel_timer(self._close_timer_name)
            except Exception as e:
                self.logger.warning(f"Error canceling close timer: {e}")

    def on_reset_safe(self):
        """Reset strategy state."""
        self.logger.info("SimpleIntervalTrader resetting...")
        self.last_buy_time = None
        self._close_timer_name = None

    # =========================================================================
    # STRATEGY LOGIC (Bar-driven entry)
    # =========================================================================

    def on_bar_safe(self, bar: Bar):
        """
        Called on each bar close.
        Check if it's time to enter based on interval.
        """
        self.logger.info(f"Bar received: {bar.bar_type} @ {bar.close}")
        
        if not self.strategy_config.enabled:
            return
        
        # Convert bar timestamp to datetime (nanoseconds to seconds)
        bar_time = datetime.utcfromtimestamp(bar.ts_event / 1_000_000_000)
        
        # Check if this bar closes on our interval boundary
        if self._should_enter(bar_time):
            self._generate_entry_signal()
            
    def _should_enter(self, bar_time: datetime) -> bool:
        """
        Determine if entry signal should be generated.
        
        Entry conditions:
        1. Bar close time aligns with interval (e.g., :00, :05, :10 for 5min interval)
        2. Haven't bought in this minute already (prevent double entry)
        3. No position currently open (checked by base.can_submit_entry_order)
        """
        # Check if minute aligns with interval
        if bar_time.minute % self.buy_interval_minutes != 0:
            return False
        
        # Check if we already bought in this minute
        if self.last_buy_time:
            if (self.last_buy_time.hour == bar_time.hour and 
                self.last_buy_time.minute == bar_time.minute):
                return False
        
        return True

    def _generate_entry_signal(self):
        """
        Generate entry signal and submit order.
        Base class handles duplicate prevention.
        """
        self.logger.info("Entry signal generated")
        
        # Create market buy order
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_int(int(self.strategy_config.order_size)),
        )
        
        # Base class validates and submits
        if self.submit_entry_order(order):
            self.last_buy_time = self.clock.utc_now()
            self.save_state()

    # =========================================================================
    # STRATEGY LOGIC (Time-based exit)
    # =========================================================================

    def on_order_filled_safe(self, event):
        """
        Called after base processes fill.
        For entry fills: schedule exit after hold duration.
        """
        if event.order_side == OrderSide.BUY:
            self._schedule_exit()

    def _schedule_exit(self):
        """Schedule position close after hold duration."""
        close_time = self.clock.utc_now() + timedelta(minutes=self.hold_duration_minutes)
        self._close_timer_name = f"{self.id}.close_position"
        
        self.clock.set_time_alert(
            name=self._close_timer_name,
            alert_time=close_time,
            callback=self._execute_exit,
            override=True
        )
        
        self.logger.info(f"Scheduled position close at {close_time}")
        self.save_state()

    def _execute_exit(self, alert):
        """
        Execute exit when timer fires.
        Base class handles order validation and submission.
        """
        self.logger.info("Hold duration expired - generating exit signal")
        self._close_timer_name = None
        
        # Use base class method to close position
        self.close_strategy_position(reason="HOLD_DURATION_EXPIRED")

    # =========================================================================
    # STATE PERSISTENCE
    # =========================================================================

    def get_state(self) -> Dict[str, Any]:
        """Return strategy-specific state."""
        return {
            "last_buy_time": self.last_buy_time.isoformat() if self.last_buy_time else None,
            "has_scheduled_close": self._close_timer_name is not None,
        }

    def set_state(self, state: Dict[str, Any]):
        """Restore strategy-specific state variables."""
        # Restore last buy time
        last_buy_str = state.get("last_buy_time")
        if last_buy_str:
            self.last_buy_time = datetime.fromisoformat(last_buy_str)
        
        # Flags for recovery logic in on_start_safe
        self._had_scheduled_close = state.get("has_scheduled_close", False)

    def _recover_state_logic(self):
        """
        Handle position recovery after crash/restart.
        Called from on_start_safe when RUNNING and instrument is ready.
        """
        if self._has_open_position():
            if self._had_scheduled_close and self.last_buy_time:
                # Calculate time remaining
                elapsed = (self.clock.utc_now() - self.last_buy_time).total_seconds() / 60
                remaining = self.hold_duration_minutes - elapsed
                
                if remaining > 0:
                    # Reschedule with remaining time
                    self.logger.info(f"Rescheduling close timer ({remaining:.1f}m remaining)")
                    self._close_timer_name = f"{self.id}.close_position"
                    self.clock.set_time_alert(
                        name=self._close_timer_name,
                        alert_time=self.clock.utc_now() + timedelta(minutes=remaining),
                        callback=self._execute_exit,
                        override=True
                    )
                else:
                    # Should have closed already - close immediately
                    self.logger.warning("Position overdue for close - closing now")
                    self.close_strategy_position(reason="OVERDUE_AFTER_RESTART")
            else:
                # Position exists but no valid scheduled close in state
                # Close it to start fresh and avoid "stuck" strategy
                pos = self._get_open_position()
                if pos:
                    self.logger.warning(f"Unknown open position found ({pos.id}) - closing to start fresh")
                    self.close_strategy_position(reason="UNKNOWN_POSITION_RESTART")
        
        # Clear recovery flag
        self._had_scheduled_close = False
