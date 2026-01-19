"""
MES Opening Range Breakout Strategy

Entry Rules:
- Opening Range: 09:30-09:45 ET
- Long if close > OR_high
- Short if close < OR_low
- Only first breakout per day

Risk Management:
- Only trade if OR width >= 0.5 * ATR(14)
- Initial stop: 1.25 * ATR
- Trailing stop: 3.0 * ATR (software-managed, not broker orders)

Overnight Hold Conditions:
- Price > EMA(200) on 30-min bars
- ADX > 20

Exit Rules:
- Software stop hit OR 15:55 ET (forced flat for day trades)
- Overnight positions exit at next day's open
- Maximum 1 trade per day
"""

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional, Dict, Any
import pytz

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce, OrderType, PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.orders import MarketOrder
from nautilus_trader.model.position import Position
from nautilus_trader.indicators import (
    AverageTrueRange, 
    ExponentialMovingAverage, 
    DirectionalMovement
)

from ..base import BaseStrategy
from ..config import StrategyConfig


class MesOrbStrategy(BaseStrategy):
    """
    MES Opening Range Breakout Strategy
    
    Uses software-managed stops instead of broker stop orders to avoid
    order management complexity and rate limiting issues.
    """
    
    def __init__(self, config: StrategyConfig, integration_manager=None, persistence_manager=None):
        super().__init__(config, integration_manager, persistence_manager)
        
        # Strategy parameters from config
        params = config.parameters
        self.or_period_minutes = params.get("or_period_minutes", 15)
        self.or_start_time = time(9, 30)  # 09:30 ET
        # Calculate OR end time based on start + minutes
        dt = datetime.combine(datetime.today(), self.or_start_time) + timedelta(minutes=self.or_period_minutes)
        self.or_end_time = dt.time()
        self.exit_time = time(15, 55)     # 15:55 ET
        
        # Configurable parameters
        self.atr_period = params.get("atr_period", 14)
        self.or_atr_multiplier = params.get("or_atr_multiplier", 0.5)
        self.initial_stop_atr_multiplier = params.get("initial_stop_atr_multiplier", 1.25)
        self.trailing_stop_atr_multiplier = params.get("trailing_stop_atr_multiplier", 3.0)
        self.ema_period = params.get("ema_period", 200)
        self.adx_period = params.get("adx_period", 14)
        self.adx_threshold = params.get("adx_threshold", 20)
        
        # Bar types
        self.bar_type_1min = None  # For ATR and OR calculation
        self.bar_type_30min = None  # For EMA and ADX
        
        # Indicators
        self.atr: Optional[AverageTrueRange] = None
        self.ema: Optional[ExponentialMovingAverage] = None
        self.dm: Optional[DirectionalMovement] = None  # Replaces ADX
        
        # Opening Range tracking
        self.or_high: Optional[float] = None
        self.or_low: Optional[float] = None
        self.or_complete = False
        self.or_bars = []
        
        # Trade tracking
        self.traded_today = False
        self.current_trade_date: Optional[datetime] = None
        self.entry_price: Optional[float] = None
        self.is_overnight_hold = False
        
        # Software stop management (no broker stop orders)
        self.stop_price: Optional[float] = None
        self.position_side: Optional[PositionSide] = None
        self.highest_price_since_entry: Optional[float] = None
        self.lowest_price_since_entry: Optional[float] = None
        
        # Timezone
        self.et_tz = pytz.timezone('America/New_York')
    
    def on_start_safe(self):
        """Initialize strategy when started."""
        if not self.instrument:
            self.logger.warning("Instrument not ready, waiting...")
            return
        
        # Create bar types
        self.bar_type_1min = BarType.from_str(f"{self.instrument_id}-1-MINUTE-LAST-EXTERNAL")
        self.bar_type_30min = BarType.from_str(f"{self.instrument_id}-30-MINUTE-LAST-EXTERNAL")
        
        # Initialize indicators
        self.atr = AverageTrueRange(self.atr_period)
        self.ema = ExponentialMovingAverage(self.ema_period)
        self.dm = DirectionalMovement(self.adx_period)
        
        # Register indicators for automatic updates
        self.register_indicator_for_bars(self.bar_type_1min, self.atr)
        self.register_indicator_for_bars(self.bar_type_30min, self.ema)
        self.register_indicator_for_bars(self.bar_type_30min, self.dm)
        
        # Subscribe to bars
        self.subscribe_bars(self.bar_type_1min)
        self.subscribe_bars(self.bar_type_30min)
        
        self.logger.info(f"MES ORB Strategy started: ATR={self.atr_period}, EMA={self.ema_period}, ADX={self.adx_period}")
    
    def on_bar(self, bar: Bar):
        """Handle incoming bars."""
        if bar.bar_type == self.bar_type_1min:
            self._handle_1min_bar(bar)

    def _handle_1min_bar(self, bar: Bar):
        """Process 1-minute bars for OR, ATR, and stop management."""
        # Get current time in ET
        dt_utc = datetime.fromtimestamp(bar.ts_event / 1_000_000_000, tz=timezone.utc)
        bar_time_et = dt_utc.astimezone(self.et_tz).time()
        bar_date = dt_utc.astimezone(self.et_tz).date()
        
        # Reset daily tracking at market open
        if bar_time_et == self.or_start_time:
            self._reset_daily_state(bar_date)
        
        # Collect OR bars
        if not self.or_complete and self.or_start_time <= bar_time_et < self.or_end_time:
            self.or_bars.append(bar)
            self.logger.debug(f"OR bar collected: {len(self.or_bars)}")
        
        # Calculate OR at end of window
        if not self.or_complete and bar_time_et >= self.or_end_time and len(self.or_bars) > 0:
            self._calculate_opening_range()
        
        # Check for entry if OR is complete and no trade today
        if self.or_complete and not self.traded_today and not self._has_open_position():
            self._check_entry(bar)
        
        # SOFTWARE STOP: Check if stop is triggered
        if self._has_open_position() and self.stop_price is not None:
            self._check_stop_triggered(bar)
        
        # Update trailing stop level (just the price, no orders)
        if self._has_open_position() and self.stop_price is not None:
            self._update_trailing_stop_level(bar)
        
        # Force exit at 15:55 if day trade
        if bar_time_et >= self.exit_time and self._has_open_position() and not self.is_overnight_hold:
            self.logger.info("Forcing exit at 15:55 ET (day trade)")
            self._exit_position("END_OF_DAY")
    
    def _reset_daily_state(self, trade_date):
        """Reset state for new trading day."""
        self.logger.info(f"Resetting daily state for {trade_date}")
        self.or_high = None
        self.or_low = None
        self.or_complete = False
        self.or_bars = []
        self.traded_today = False
        self.current_trade_date = trade_date
        
        # If holding overnight position, exit at open
        if self.is_overnight_hold and self._has_open_position():
            self.logger.info("Exiting overnight position at market open")
            self._exit_position("OVERNIGHT_CLOSE")
            self.is_overnight_hold = False
            # Ensure this exit doesn't count as the "day trade" for today
            self.traded_today = False
    
    def _calculate_opening_range(self):
        """Calculate opening range high/low."""
        if not self.or_bars:
            return
        
        self.or_high = max(float(bar.high) for bar in self.or_bars)
        self.or_low = min(float(bar.low) for bar in self.or_bars)
        or_width = self.or_high - self.or_low
        
        # Check if OR is wide enough (>= 0.5 * ATR)
        if self.atr.initialized:
            min_width = self.atr.value * self.or_atr_multiplier
            if or_width < min_width:
                self.logger.info(f"OR too narrow: {or_width:.2f} < {min_width:.2f}, skipping today")
                self.traded_today = True  # Mark as traded to prevent entries
                return
        
        self.or_complete = True
        self.logger.info(f"OR calculated: High={self.or_high:.2f}, Low={self.or_low:.2f}, Width={or_width:.2f}")
    
    def _check_entry(self, bar: Bar):
        """Check for breakout entry."""
        if not self.atr.initialized:
            return
        
        close = float(bar.close)
        
        # Long entry: close > OR_high
        if close > self.or_high:
            self.logger.info(f"Long breakout detected: {close:.2f} > {self.or_high:.2f}")
            self._enter_position(OrderSide.BUY, close)
        
        # Short entry: close < OR_low
        elif close < self.or_low:
            self.logger.info(f"Short breakout detected: {close:.2f} < {self.or_low:.2f}")
            self._enter_position(OrderSide.SELL, close)
    
    def _enter_position(self, side: OrderSide, entry_price: float):
        """Enter a position and set initial software stop."""
        self.traded_today = True
        self.entry_price = entry_price
        
        # Calculate initial stop distance
        stop_distance = self.atr.value * self.initial_stop_atr_multiplier
        
        # Set position side and initial stop price
        if side == OrderSide.BUY:
            self.position_side = PositionSide.LONG
            self.stop_price = entry_price - stop_distance
            self.highest_price_since_entry = entry_price
            self.lowest_price_since_entry = None
        else:
            self.position_side = PositionSide.SHORT
            self.stop_price = entry_price + stop_distance
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
            f"initial stop at {self.stop_price:.2f} (distance: {stop_distance:.2f})"
        )
    
    def _check_stop_triggered(self, bar: Bar):
        """Check if software stop price was hit and exit if so."""
        if self.stop_price is None or self.position_side is None:
            return
        
        current = float(bar.close)
        high = float(bar.high)
        low = float(bar.low)
        
        triggered = False
        
        if self.position_side == PositionSide.LONG:
            # Check if low touched stop (more accurate than just close)
            if low <= self.stop_price:
                triggered = True
                exit_reason = "TRAILING_STOP" if self._is_trailing_active() else "STOP_LOSS"
                self.logger.info(
                    f"Stop triggered (LONG): low={low:.2f} <= stop={self.stop_price:.2f}"
                )
        else:  # SHORT
            # Check if high touched stop
            if high >= self.stop_price:
                triggered = True
                exit_reason = "TRAILING_STOP" if self._is_trailing_active() else "STOP_LOSS"
                self.logger.info(
                    f"Stop triggered (SHORT): high={high:.2f} >= stop={self.stop_price:.2f}"
                )
        
        if triggered:
            self._exit_position(exit_reason)
    
    def _is_trailing_active(self) -> bool:
        """Check if the stop has been trailed from initial level."""
        if self.entry_price is None or self.stop_price is None:
            return False
        
        initial_distance = self.atr.value * self.initial_stop_atr_multiplier if self.atr and self.atr.initialized else 0
        
        if self.position_side == PositionSide.LONG:
            initial_stop = self.entry_price - initial_distance
            return self.stop_price > initial_stop + 0.01  # Small tolerance
        else:
            initial_stop = self.entry_price + initial_distance
            return self.stop_price < initial_stop - 0.01
    
    def _update_trailing_stop_level(self, bar: Bar):
        """Update the trailing stop PRICE (no broker orders)."""
        if self.stop_price is None or self.position_side is None:
            return
        
        if not self.atr or not self.atr.initialized:
            return
        
        current = float(bar.close)
        trailing_distance = self.atr.value * self.trailing_stop_atr_multiplier
        
        if self.position_side == PositionSide.LONG:
            # Update highest price
            if self.highest_price_since_entry is None or current > self.highest_price_since_entry:
                self.highest_price_since_entry = current
            
            # Calculate new trailing stop
            new_stop = self.highest_price_since_entry - trailing_distance
            
            # Only raise stop (never lower for longs)
            if new_stop > self.stop_price:
                old_stop = self.stop_price
                self.stop_price = new_stop
                self.logger.debug(f"Trailing stop raised: {old_stop:.2f} -> {self.stop_price:.2f}")
        
        else:  # SHORT
            # Update lowest price
            if self.lowest_price_since_entry is None or current < self.lowest_price_since_entry:
                self.lowest_price_since_entry = current
            
            # Calculate new trailing stop
            new_stop = self.lowest_price_since_entry + trailing_distance
            
            # Only lower stop (never raise for shorts)
            if new_stop < self.stop_price:
                old_stop = self.stop_price
                self.stop_price = new_stop
                self.logger.debug(f"Trailing stop lowered: {old_stop:.2f} -> {self.stop_price:.2f}")
    
    def _exit_position(self, reason: str):
        """Exit the current position and clear stop state."""
        self._last_exit_reason = reason
        self.close_strategy_position(reason=reason)
        self._clear_stop_state()
    
    def _clear_stop_state(self):
        """Clear all stop-related state after position is closed."""
        self.stop_price = None
        self.position_side = None
        self.highest_price_since_entry = None
        self.lowest_price_since_entry = None
        self.entry_price = None
    
    def _check_overnight_hold_conditions(self) -> bool:
        """Check if position should be held overnight."""
        if not self.ema.initialized or not self.dm.initialized:
            return False
        
        position = self._get_open_position()
        if not position:
            return False
        
        # Get current price
        current_price = float(position.last_px) if position.last_px else self.entry_price
        
        # Check conditions: Price > EMA(200) AND ADX > 20
        if position.side == PositionSide.LONG:
            price_above_ema = current_price > self.ema.value
            adx_strong = self.dm.adx > self.adx_threshold
            
            return price_above_ema and adx_strong
        
        return False
    
    def on_order_filled(self, event):
        """Handle order fills - sync position side from actual fill."""
        # Sync position side from actual fill event
        if event.order_side == OrderSide.BUY:
            # Could be entry (LONG) or exit (closing SHORT)
            if self.position_side is None:
                self.position_side = PositionSide.LONG
        else:  # SELL
            if self.position_side is None:
                self.position_side = PositionSide.SHORT
        
        # Check if this is an exit fill (position closed)
        pos = self._get_open_position()
        if pos is None:
            self.logger.info("Position closed, clearing stop state")
            self._clear_stop_state()
        
        super().on_order_filled(event)
    
    def get_state(self) -> Dict[str, Any]:
        """Return strategy state for persistence."""
        return {
            "or_high": self.or_high,
            "or_low": self.or_low,
            "or_complete": self.or_complete,
            "traded_today": self.traded_today,
            "current_trade_date": self.current_trade_date.isoformat() if self.current_trade_date else None,
            "entry_price": self.entry_price,
            "is_overnight_hold": self.is_overnight_hold,
            # Software stop state
            "stop_price": self.stop_price,
            "position_side": self.position_side.name if self.position_side else None,
            "highest_price_since_entry": self.highest_price_since_entry,
            "lowest_price_since_entry": self.lowest_price_since_entry,
        }
    
    def set_state(self, state: Dict[str, Any]):
        """Restore strategy state from persistence."""
        self.or_high = state.get("or_high")
        self.or_low = state.get("or_low")
        self.or_complete = state.get("or_complete", False)
        self.traded_today = state.get("traded_today", False)
        
        date_str = state.get("current_trade_date")
        if date_str:
            self.current_trade_date = datetime.fromisoformat(date_str).date()
        
        self.entry_price = state.get("entry_price")
        self.is_overnight_hold = state.get("is_overnight_hold", False)
        
        # Restore software stop state
        self.stop_price = state.get("stop_price")
        position_side_str = state.get("position_side")
        if position_side_str:
            self.position_side = PositionSide[position_side_str]
        else:
            self.position_side = None
        self.highest_price_since_entry = state.get("highest_price_since_entry")
        self.lowest_price_since_entry = state.get("lowest_price_since_entry")
    
    def on_stop_safe(self):
        """Cleanup when strategy stops."""
        self.logger.info("MES ORB Strategy stopping...")
