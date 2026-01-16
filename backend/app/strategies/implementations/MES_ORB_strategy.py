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
- Trailing stop: 3.0 * ATR

Overnight Hold Conditions:
- Price > EMA(200) on 30-min bars
- ADX > 20

Exit Rules:
- Stop hit OR 15:55 ET (forced flat for day trades)
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
from nautilus_trader.model.orders import MarketOrder, StopMarketOrder
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
        self.stop_order_id: Optional[str] = None
        self.highest_price_since_entry: Optional[float] = None
        self.lowest_price_since_entry: Optional[float] = None
        self.is_overnight_hold = False
        
        # Timezone
        self.et_tz = pytz.timezone('America/New_York')
    
    def on_start_safe(self):
        """Initialize strategy when started"""
        if not self.instrument:
            self.logger.warning("Instrument not ready, waiting...")
            return
        
        # Create bar types
        self.bar_type_1min = BarType.from_str(f"{self.instrument_id}-1-MINUTE-LAST-INTERNAL")
        self.bar_type_30min = BarType.from_str(f"{self.instrument_id}-30-MINUTE-LAST-INTERNAL")
        
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
        """Handle incoming bars"""
        if bar.bar_type == self.bar_type_1min:
            self._handle_1min_bar(bar)

    def _handle_1min_bar(self, bar: Bar):
        """Process 1-minute bars for OR and ATR"""
        # Note: ATR updated automatically
        
        # Get current time in ET
        # bar.ts_event is int (ns), convert to datetime
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
        if self.or_complete and not self.traded_today and not self._has_open_position:
            self._check_entry(bar)
        
        # Update trailing stops if in position
        if self._has_open_position:
            self._update_trailing_stop(bar)
        
        # Force exit at 15:55 if day trade
        # Force exit at 15:55 if day trade
        if bar_time_et >= self.exit_time and self._has_open_position and not self.is_overnight_hold:
            self.logger.info("Forcing exit at 15:55 ET (day trade)")
            self.close_strategy_position(reason="END_OF_DAY")
    
    
    def _reset_daily_state(self, trade_date):
        """Reset state for new trading day"""
        self.logger.info(f"Resetting daily state for {trade_date}")
        self.or_high = None
        self.or_low = None
        self.or_complete = False
        self.or_bars = []
        self.traded_today = False
        self.current_trade_date = trade_date
        
        # If holding overnight position, exit at open
        if self.is_overnight_hold and self._has_open_position:
            self.logger.info("Exiting overnight position at market open")
            # Using close_strategy_position handles logging and persistence
            self.close_strategy_position(reason="OVERNIGHT_CLOSE")
            self.is_overnight_hold = False
            # Ensure this exit doesn't count as the "day trade" for today
            self.traded_today = False
    
    def _calculate_opening_range(self):
        """Calculate opening range high/low"""
        if not self.or_bars:
            return
        
        self.or_high = max(float(bar.high) for bar in self.or_bars)
        self.or_low = min(float(bar.low) for bar in self.or_bars)
        or_width = self.or_high - self.or_low
        
        # Check if OR is wide enough (>= 0.5 * ATR)
        if self.atr.initialized:
            min_width = 0.5 * self.atr.value * self.or_atr_multiplier
            if or_width < min_width:
                self.logger.info(f"OR too narrow: {or_width:.2f} < {min_width:.2f}, skipping today")
                self.traded_today = True  # Mark as traded to prevent entries
                return
        
        self.or_complete = True
        self.logger.info(f"OR calculated: High={self.or_high:.2f}, Low={self.or_low:.2f}, Width={or_width:.2f}")
    
    def _check_entry(self, bar: Bar):
        """Check for breakout entry"""
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
        """Enter a position with initial stop"""
        self.traded_today = True
        self.entry_price = entry_price
        
        # Calculate initial stop
        stop_distance = self.atr.value * self.initial_stop_atr_multiplier
        
        if side == OrderSide.BUY:
            stop_price = entry_price - stop_distance
            self.highest_price_since_entry = entry_price
        else:
            stop_price = entry_price + stop_distance
            self.lowest_price_since_entry = entry_price
        
        # Submit market order
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=self.instrument.make_qty(self.strategy_config.order_size)
        )
        
        self.submit_order(order)
        self.logger.info(f"Entered {side.name} at {entry_price:.2f}, initial stop at {stop_price:.2f}")
        
        # Place initial stop order
        self._place_stop_order(stop_price, side)
    
    def on_order_filled(self, event):
        """Override to capture proper exit reason from stop orders"""
        if self.stop_order_id and str(event.client_order_id) == self.stop_order_id:
            # Determine if this was an initial stop or trailing stop
            # We can check if highest/lowest price moved locally since entry
            is_trailing = False
            if self.entry_price:
                dist = abs(self.entry_price - float(event.last_px))
                initial_dist = self.atr.value * self.initial_stop_atr_multiplier if self.atr and self.atr.initialized else 0
                # If distance is significantly different from initial stop, it was likely trailed
                # Or simplistically, if we ever updated the stop order, it's a trailing stop.
                # Since we don't track update count easily here, let's use the price check
                pass

            # Simpler approach: If we ever updated the trailing stop, we can set a flag
            # For now, let's just default to STOP_LOSS, unless we know we trailed
            reason = "STOP_LOSS"
            
            # If we updated the stop order, it's a trailing stop
            # We can track this in _update_trailing_stop
            if getattr(self, "_is_trailing_active", False):
                reason = "TRAILING_STOP"
                
            self._last_exit_reason = reason
            self.logger.info(f"Stop order filled. Reason set to: {reason}")

        super().on_order_filled(event)

    def _place_stop_order(self, stop_price: float, position_side: OrderSide):
        """Place or update stop order"""
        # Cancel existing stop if any
        if self.stop_order_id:
            # Cancel logic here - checking cache for cancellable order
            # For now assuming we just place new one or modify
            # In a real impl we'd cancel the old one first
            pass
        
        # Determine stop order side (opposite of position)
        stop_side = OrderSide.SELL if position_side == OrderSide.BUY else OrderSide.BUY
        
        # Create stop order
        stop_order = self.order_factory.stop_market(
            instrument_id=self.instrument_id,
            order_side=stop_side,
            quantity=self.instrument.make_qty(self.strategy_config.order_size),
            trigger_price=self.instrument.make_price(stop_price)
        )
        
        self.submit_order(stop_order)
        self.stop_order_id = str(stop_order.client_order_id)
        
        # If this isn't the first stop, it's likely a trailing update (or initial placement)
        # We can set a flag if this is a modification.
        if hasattr(self, "entry_price") and self.entry_price:
             # Calculate distance
             pass
             
        self.logger.info(f"Stop order placed at {stop_price:.2f}")

    def _update_trailing_stop(self, bar: Bar):
        """Update trailing stop based on price movement"""
        if not self._has_open_position:
            return
        
        position = self._get_open_position()
        if not position:
            return
        
        current_price = float(bar.close)
        trailing_distance = self.atr.value * self.trailing_stop_atr_multiplier
        
        updated = False
        if position.side == PositionSide.LONG:
            # Update highest price
            if self.highest_price_since_entry is None or current_price > self.highest_price_since_entry:
                self.highest_price_since_entry = current_price
            
            # Calculate new trailing stop
            new_stop = self.highest_price_since_entry - trailing_distance
            
            # Only update if new stop is higher (trailing up)
            if self.entry_price and new_stop > (self.entry_price - self.atr.value * self.initial_stop_atr_multiplier):
                # Ensure new stop is higher than current effective stop? 
                # Ideally check existing stop order price.
                self._place_stop_order(new_stop, OrderSide.BUY)
                updated = True
        
        else:  # SHORT
            # Update lowest price
            if self.lowest_price_since_entry is None or current_price < self.lowest_price_since_entry:
                self.lowest_price_since_entry = current_price
            
            # Calculate new trailing stop
            new_stop = self.lowest_price_since_entry + trailing_distance
            
            # Only update if new stop is lower (trailing down)
            if self.entry_price and new_stop < (self.entry_price + self.atr.value * self.initial_stop_atr_multiplier):
                self._place_stop_order(new_stop, OrderSide.SELL)
                updated = True
                
        if updated:
            self._is_trailing_active = True
    
    def _check_overnight_hold_conditions(self) -> bool:
        """Check if position should be held overnight"""
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
    
    def _close_position(self, reason: str):
        """Deprecated: Use close_strategy_position instead"""
        self.close_strategy_position(reason=reason)
    
    @property
    def _has_open_position(self) -> bool:
        """Check if strategy has an open position"""
        position = self._get_open_position()
        return position is not None and not position.is_closed
    
    def _get_open_position(self) -> Optional[Position]:
        """Get current open position for this instrument"""
        positions = self.cache.positions_open(instrument_id=self.instrument_id)
        return positions[0] if positions else None
    
    def get_state(self) -> Dict[str, Any]:
        """Return strategy state for persistence"""
        return {
            "or_high": self.or_high,
            "or_low": self.or_low,
            "or_complete": self.or_complete,
            "traded_today": self.traded_today,
            "current_trade_date": self.current_trade_date.isoformat() if self.current_trade_date else None,
            "entry_price": self.entry_price,
            "is_overnight_hold": self.is_overnight_hold
        }
    
    def set_state(self, state: Dict[str, Any]):
        """Restore strategy state from persistence"""
        self.or_high = state.get("or_high")
        self.or_low = state.get("or_low")
        self.or_complete = state.get("or_complete", False)
        self.traded_today = state.get("traded_today", False)
        
        date_str = state.get("current_trade_date")
        if date_str:
            self.current_trade_date = datetime.fromisoformat(date_str).date()
        
        self.entry_price = state.get("entry_price")
        self.is_overnight_hold = state.get("is_overnight_hold", False)
    
    def on_stop_safe(self):
        """Cleanup when strategy stops"""
        self.logger.info("MES ORB Strategy stopping...")
