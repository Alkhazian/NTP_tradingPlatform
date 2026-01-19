"""
MES Opening Range Breakout Strategy - Simplified Version

A streamlined ORB strategy with fixed point-based stops and retest entry confirmation.

Entry Rules:
- Opening Range: 9:30-9:45 AM ET (15 minutes)
- Trading Window: 9:50 AM - 3:45 PM ET
- Long entry: OR high broken, price retests OR high, then closes above
- Short entry: OR low broken, price retests OR low, then closes below
- Maximum 1 trade per day

Risk Management (Fixed Points):
- Initial Stop Loss: 15 points
- Trailing Stop: 30 points from high/low
- Trail Offset: 5 points (trailing starts after 5 points profit)

Exit Rules:
- Stop hit OR 3:45 PM ET (forced flat)
"""

from datetime import datetime, time, timedelta, timezone
from typing import Optional, Dict, Any
import pytz

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.position import Position

from ..base import BaseStrategy
from ..config import StrategyConfig


class MesOrbSimpleStrategy(BaseStrategy):
    """
    Simplified MES Opening Range Breakout Strategy
    
    Uses fixed point-based stops and requires a retest of the OR level
    before entry for confirmation.
    """
    
    def __init__(self, config: StrategyConfig, integration_manager=None, persistence_manager=None):
        super().__init__(config, integration_manager, persistence_manager)
        
        # Strategy parameters from config
        params = config.parameters
        
        # Fixed point-based stops (configurable)
        self.initial_stop_points = params.get("initial_stop_points", 15.0)
        self.trailing_stop_points = params.get("trailing_stop_points", 30.0)
        self.trail_offset_points = params.get("trail_offset_points", 5.0)
        
        # Time settings
        self.or_start_time = time(9, 30)   # 09:30 ET
        self.or_end_time = time(9, 45)     # 09:45 ET
        self.trade_start_time = time(9, 50)  # 09:50 ET - start looking for entries
        self.exit_time = time(15, 45)      # 15:45 ET - forced flat
        
        # Bar type
        self.bar_type_1min = None
        
        # Opening Range tracking
        self.or_high: Optional[float] = None
        self.or_low: Optional[float] = None
        self.or_complete = False
        self.or_bars = []
        
        # Breakout and retest tracking
        self.high_broken = False  # OR high was broken
        self.low_broken = False   # OR low was broken
        self.high_retested = False  # Price came back to OR high after breaking
        self.low_retested = False   # Price came back to OR low after breaking
        
        # Trade tracking
        self.traded_today = False
        self.current_trade_date: Optional[datetime] = None
        
        # Software stop management
        self.entry_price: Optional[float] = None
        self.stop_price: Optional[float] = None
        self.position_side: Optional[PositionSide] = None
        self.highest_price_since_entry: Optional[float] = None
        self.lowest_price_since_entry: Optional[float] = None
        self.trailing_active = False  # True once trail offset is reached
        
        # Timezone
        self.et_tz = pytz.timezone('America/New_York')
    
    def on_start_safe(self):
        """Initialize strategy when started."""
        if not self.instrument:
            self.logger.warning("Instrument not ready, waiting...")
            return
        
        # Create bar type
        self.bar_type_1min = BarType.from_str(f"{self.instrument_id}-1-MINUTE-LAST-INTERNAL")
        
        # Subscribe to bars
        self.subscribe_bars(self.bar_type_1min)
        
        self.logger.info(
            f"MES ORB Simple Strategy started: "
            f"Stop={self.initial_stop_points}pts, Trail={self.trailing_stop_points}pts, "
            f"Offset={self.trail_offset_points}pts"
        )
    
    def on_bar(self, bar: Bar):
        """Handle incoming bars."""
        if bar.bar_type == self.bar_type_1min:
            self._handle_bar(bar)

    def _handle_bar(self, bar: Bar):
        """Process 1-minute bars."""
        # Get current time in ET
        dt_utc = datetime.fromtimestamp(bar.ts_event / 1_000_000_000, tz=timezone.utc)
        bar_time_et = dt_utc.astimezone(self.et_tz).time()
        bar_date = dt_utc.astimezone(self.et_tz).date()
        
        # Reset daily tracking at market open
        if bar_time_et == self.or_start_time:
            self._reset_daily_state(bar_date)
        
        # Collect OR bars (9:30 - 9:45)
        if not self.or_complete and self.or_start_time <= bar_time_et < self.or_end_time:
            self.or_bars.append(bar)
            self.logger.debug(f"OR bar collected: {len(self.or_bars)}")
        
        # Calculate OR at end of window (9:45)
        if not self.or_complete and bar_time_et >= self.or_end_time and len(self.or_bars) > 0:
            self._calculate_opening_range()
        
        # During trading window (9:50 - 15:45)
        if self.or_complete and self.trade_start_time <= bar_time_et < self.exit_time:
            # Track breakouts and retests
            if not self.traded_today and not self._has_open_position:
                self._track_breakout_retest(bar)
                self._check_entry(bar)
            
            # Manage stops if in position
            if self._has_open_position and self.stop_price is not None:
                self._check_stop_triggered(bar)
                self._update_trailing_stop(bar)
        
        # Force exit at 15:45
        if bar_time_et >= self.exit_time and self._has_open_position:
            self.logger.info("Forcing exit at 15:45 ET")
            self._exit_position("END_OF_DAY")
    
    def _reset_daily_state(self, trade_date):
        """Reset state for new trading day."""
        self.logger.info(f"Resetting daily state for {trade_date}")
        
        # Clear OR tracking
        self.or_high = None
        self.or_low = None
        self.or_complete = False
        self.or_bars = []
        
        # Clear breakout/retest tracking
        self.high_broken = False
        self.low_broken = False
        self.high_retested = False
        self.low_retested = False
        
        # Clear trade tracking
        self.traded_today = False
        self.current_trade_date = trade_date
        
        # Clear stop state
        self._clear_stop_state()
    
    def _calculate_opening_range(self):
        """Calculate opening range high/low."""
        if not self.or_bars:
            return
        
        self.or_high = max(float(bar.high) for bar in self.or_bars)
        self.or_low = min(float(bar.low) for bar in self.or_bars)
        or_width = self.or_high - self.or_low
        
        self.or_complete = True
        self.logger.info(
            f"OR calculated: High={self.or_high:.2f}, Low={self.or_low:.2f}, "
            f"Width={or_width:.2f} pts"
        )
    
    def _track_breakout_retest(self, bar: Bar):
        """Track breakout and retest conditions."""
        high = float(bar.high)
        low = float(bar.low)
        close = float(bar.close)
        
        # Track high breakout
        if not self.high_broken and high > self.or_high:
            self.high_broken = True
            self.logger.info(f"OR HIGH broken: {high:.2f} > {self.or_high:.2f}")
        
        # Track high retest (price comes back to touch OR high after breaking)
        if self.high_broken and not self.high_retested:
            if low <= self.or_high:
                self.high_retested = True
                self.logger.info(f"OR HIGH retested: low={low:.2f} touched {self.or_high:.2f}")
        
        # Track low breakout
        if not self.low_broken and low < self.or_low:
            self.low_broken = True
            self.logger.info(f"OR LOW broken: {low:.2f} < {self.or_low:.2f}")
        
        # Track low retest (price comes back to touch OR low after breaking)
        if self.low_broken and not self.low_retested:
            if high >= self.or_low:
                self.low_retested = True
                self.logger.info(f"OR LOW retested: high={high:.2f} touched {self.or_low:.2f}")
    
    def _check_entry(self, bar: Bar):
        """Check for entry signals after breakout + retest."""
        close = float(bar.close)
        
        # Long entry: OR high broken + retested + close above OR high
        if self.high_broken and self.high_retested and close > self.or_high:
            # Make sure we haven't also triggered short conditions
            if not (self.low_broken and self.low_retested):
                self.logger.info(
                    f"LONG signal: broken + retested + close={close:.2f} > OR_high={self.or_high:.2f}"
                )
                self._enter_position(OrderSide.BUY, close)
                return
        
        # Short entry: OR low broken + retested + close below OR low
        if self.low_broken and self.low_retested and close < self.or_low:
            # Make sure we haven't also triggered long conditions
            if not (self.high_broken and self.high_retested):
                self.logger.info(
                    f"SHORT signal: broken + retested + close={close:.2f} < OR_low={self.or_low:.2f}"
                )
                self._enter_position(OrderSide.SELL, close)
                return
    
    def _enter_position(self, side: OrderSide, entry_price: float):
        """Enter a position and set initial stop."""
        self.traded_today = True
        self.entry_price = entry_price
        self.trailing_active = False
        
        # Set position side and initial stop
        if side == OrderSide.BUY:
            self.position_side = PositionSide.LONG
            self.stop_price = entry_price - self.initial_stop_points
            self.highest_price_since_entry = entry_price
            self.lowest_price_since_entry = None
        else:
            self.position_side = PositionSide.SHORT
            self.stop_price = entry_price + self.initial_stop_points
            self.lowest_price_since_entry = entry_price
            self.highest_price_since_entry = None
        
        # Submit market order
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=self.instrument.make_qty(self.strategy_config.order_size)
        )
        
        self.submit_entry_order(order)
        self.logger.info(
            f"Entered {side.name} at {entry_price:.2f}, "
            f"stop at {self.stop_price:.2f} ({self.initial_stop_points} pts)"
        )
    
    def _check_stop_triggered(self, bar: Bar):
        """Check if stop was hit."""
        if self.stop_price is None or self.position_side is None:
            return
        
        high = float(bar.high)
        low = float(bar.low)
        
        triggered = False
        
        if self.position_side == PositionSide.LONG:
            if low <= self.stop_price:
                triggered = True
                reason = "TRAILING_STOP" if self.trailing_active else "STOP_LOSS"
                self.logger.info(f"Stop triggered (LONG): low={low:.2f} <= stop={self.stop_price:.2f}")
        else:  # SHORT
            if high >= self.stop_price:
                triggered = True
                reason = "TRAILING_STOP" if self.trailing_active else "STOP_LOSS"
                self.logger.info(f"Stop triggered (SHORT): high={high:.2f} >= stop={self.stop_price:.2f}")
        
        if triggered:
            self._exit_position(reason)
    
    def _update_trailing_stop(self, bar: Bar):
        """Update trailing stop once trail offset is reached."""
        if self.stop_price is None or self.position_side is None or self.entry_price is None:
            return
        
        current = float(bar.close)
        
        if self.position_side == PositionSide.LONG:
            # Update highest price
            if self.highest_price_since_entry is None or current > self.highest_price_since_entry:
                self.highest_price_since_entry = current
            
            # Check if trail offset reached (5 points profit)
            profit = self.highest_price_since_entry - self.entry_price
            if profit >= self.trail_offset_points:
                self.trailing_active = True
                
                # Calculate new trailing stop
                new_stop = self.highest_price_since_entry - self.trailing_stop_points
                
                # Only raise stop
                if new_stop > self.stop_price:
                    old_stop = self.stop_price
                    self.stop_price = new_stop
                    self.logger.debug(f"Trailing stop raised: {old_stop:.2f} -> {self.stop_price:.2f}")
        
        else:  # SHORT
            # Update lowest price
            if self.lowest_price_since_entry is None or current < self.lowest_price_since_entry:
                self.lowest_price_since_entry = current
            
            # Check if trail offset reached (5 points profit)
            profit = self.entry_price - self.lowest_price_since_entry
            if profit >= self.trail_offset_points:
                self.trailing_active = True
                
                # Calculate new trailing stop
                new_stop = self.lowest_price_since_entry + self.trailing_stop_points
                
                # Only lower stop
                if new_stop < self.stop_price:
                    old_stop = self.stop_price
                    self.stop_price = new_stop
                    self.logger.debug(f"Trailing stop lowered: {old_stop:.2f} -> {self.stop_price:.2f}")
    
    def _exit_position(self, reason: str):
        """Exit position and clear stop state."""
        self._last_exit_reason = reason
        self.close_strategy_position(reason=reason)
        self._clear_stop_state()
    
    def _clear_stop_state(self):
        """Clear all stop-related state."""
        self.stop_price = None
        self.position_side = None
        self.highest_price_since_entry = None
        self.lowest_price_since_entry = None
        self.entry_price = None
        self.trailing_active = False
    
    @property
    def _has_open_position(self) -> bool:
        """Check if strategy has an open position."""
        position = self._get_open_position()
        return position is not None and not position.is_closed
    
    def _get_open_position(self) -> Optional[Position]:
        """Get current open position for this instrument."""
        positions = self.cache.positions_open(instrument_id=self.instrument_id)
        return positions[0] if positions else None
    
    def on_order_filled(self, event):
        """Handle order fills."""
        # Check if position closed
        pos = self._get_open_position()
        if pos is None:
            self.logger.info("Position closed, clearing stop state")
            self._clear_stop_state()
        
        super().on_order_filled(event)
    
    def get_state(self) -> Dict[str, Any]:
        """Return strategy state for persistence."""
        return {
            # OR state
            "or_high": self.or_high,
            "or_low": self.or_low,
            "or_complete": self.or_complete,
            # Breakout/retest state
            "high_broken": self.high_broken,
            "low_broken": self.low_broken,
            "high_retested": self.high_retested,
            "low_retested": self.low_retested,
            # Trade state
            "traded_today": self.traded_today,
            "current_trade_date": self.current_trade_date.isoformat() if self.current_trade_date else None,
            # Stop state
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "position_side": self.position_side.name if self.position_side else None,
            "highest_price_since_entry": self.highest_price_since_entry,
            "lowest_price_since_entry": self.lowest_price_since_entry,
            "trailing_active": self.trailing_active,
        }
    
    def set_state(self, state: Dict[str, Any]):
        """Restore strategy state from persistence."""
        # OR state
        self.or_high = state.get("or_high")
        self.or_low = state.get("or_low")
        self.or_complete = state.get("or_complete", False)
        
        # Breakout/retest state
        self.high_broken = state.get("high_broken", False)
        self.low_broken = state.get("low_broken", False)
        self.high_retested = state.get("high_retested", False)
        self.low_retested = state.get("low_retested", False)
        
        # Trade state
        self.traded_today = state.get("traded_today", False)
        date_str = state.get("current_trade_date")
        if date_str:
            self.current_trade_date = datetime.fromisoformat(date_str).date()
        
        # Stop state
        self.entry_price = state.get("entry_price")
        self.stop_price = state.get("stop_price")
        position_side_str = state.get("position_side")
        if position_side_str:
            self.position_side = PositionSide[position_side_str]
        else:
            self.position_side = None
        self.highest_price_since_entry = state.get("highest_price_since_entry")
        self.lowest_price_since_entry = state.get("lowest_price_since_entry")
        self.trailing_active = state.get("trailing_active", False)
    
    def on_stop_safe(self):
        """Cleanup when strategy stops."""
        self.logger.info("MES ORB Simple Strategy stopping...")
