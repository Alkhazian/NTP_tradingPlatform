from decimal import Decimal
from datetime import time, datetime, timedelta
import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.trading.strategy import Strategy
from app.strategies.base import BaseStrategy, BaseStrategyConfig

class MesOrbStrategyConfig(BaseStrategyConfig):
    instrument_id: str = "MES.FUT-202403-GLOBEX"
    bar_type: str = "MES.FUT-202403-GLOBEX-1-MINUTE-MID-EXTERNAL" # Simplified string rep
    stop_loss_points: float = 10.0
    trailing_loss_points: float = 15.0
    orb_period_minutes: int = 15
    contract_quantity: int = 1
    session_start_time: str = "09:30:00" # ET

class MesOrbStrategy(BaseStrategy):
    def __init__(self, config: MesOrbStrategyConfig):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        # Parse bar type: forcing 1-minute bars for granularity
        self.bar_type = BarType.from_str(config.bar_type) 
        
        self.sl_points = Decimal(str(config.stop_loss_points))
        self.trail_points = Decimal(str(config.trailing_loss_points))
        self.qty = Decimal(str(config.contract_quantity))
        
        self.session_start = datetime.strptime(config.session_start_time, "%H:%M:%S").time()
        self.orb_end_time = (datetime.combine(datetime.today(), self.session_start) + timedelta(minutes=config.orb_period_minutes)).time()
        
        self.orb_high = None
        self.orb_low = None
        self.orb_complete = False
        
        self.entry_order = None
        self.position_id = None

    def on_start(self):
        # Nautilus `on_start` is synchronous. 
        # The Manager loaded the state into self.user_state.
        super().on_start()
        self.subscribe_bars(self.bar_type)
        
        # Subscribe to daily reset clock (e.g., 09:00 ET for reset)
        # Assuming we want to reset BEFORE the session starts
        from nautilus_trader.model.enums import TimeUnit
        # self.subscribe_clock(time(9, 0)) # Simplified, in real use might need timezone handling
        
        # Restore daily counters
        self.daily_trades = self.user_state.get("daily_trades", 0)
        self.last_reset_date = self.user_state.get("last_reset_date", "")
        
        current_date = datetime.now().strftime("%Y-%m-%d")
        if self.last_reset_date != current_date:
            self._reset_daily_state(current_date)
            
        self.log.info(f"MES ORB Strategy Started. Daily Trades: {self.daily_trades}. Mode: {self.mode.value}")

    def _reset_daily_state(self, current_date: str):
        """Resets strategy state for a new trading day."""
        self.daily_trades = 0
        self.orb_high = None
        self.orb_low = None
        self.orb_complete = False
        self.last_reset_date = current_date
        self.user_state["daily_trades"] = 0
        self.user_state["last_reset_date"] = current_date
        self.user_state["orb_high"] = None
        self.user_state["orb_low"] = None
        self.log.info(f"Daily reset performed for {current_date}")
    
    def on_bar(self, bar: Bar):
        from app.strategies.base import StrategyMode
        # 0. Mode checks
        if self.mode in [StrategyMode.STOPPED, StrategyMode.PAUSED, StrategyMode.ERROR]:
            return

        # Ensure we are in the session
        bar_time = datetime.fromtimestamp(bar.ts_event / 1e9).time()
        
        # 1. ORB Calculation Phase (Always run to keep state synced)
        if bar_time >= self.session_start and bar_time < self.orb_end_time:
            if self.orb_high is None or bar.high > self.orb_high:
                self.orb_high = bar.high
            if self.orb_low is None or bar.low < self.orb_low:
                self.orb_low = bar.low
            return

        # 2. ORB Complete - Set Levels
        if not self.orb_complete and bar_time >= self.orb_end_time:
            self.orb_complete = True
            self.log.info(f"ORB Complete. High: {self.orb_high}, Low: {self.orb_low}")
            return

        if not self.orb_complete:
            return

        # 3. Position Management
        has_position = self.portfolio.is_net_pos(self.instrument_id)

        if has_position:
             # Manage Position (Trailing Stops, etc.)
             # We allow this logic even in REDUCE_ONLY mode
             pass
        else:
             # Entry Logic
             # Skip if in REDUCE_ONLY or STOPPING mode
             if self.mode in [StrategyMode.REDUCE_ONLY, StrategyMode.STOPPING]:
                 return

             # Check constraints
             if self.daily_trades >= 1:
                 return

             # Breakout Logic implementation
             if bar.close > self.orb_high:
                 self._enter_position(OrderSide.BUY, "UP")
             elif bar.close < self.orb_low:
                 self._enter_position(OrderSide.SELL, "DOWN")

    def _enter_position(self, side: OrderSide, direction: str):
        self.log.info(f"ORB Breakout {direction} - Entering {side.name}")
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=self.qty,
        )
        self.submit_order(order)
        
        self.daily_trades += 1
        self.user_state["daily_trades"] = self.daily_trades
        import asyncio
        asyncio.create_task(self.save_state())

    def on_stop(self):
        """Cleanup on stop."""
        self.log.info("Cleaning up strategy resources...")
        if self.mode == StrategyMode.STOPPING:
             # Potentially more aggressive cleanup if not just a normal stop
             pass
        super().on_stop()
        self.cancel_all_orders(self.instrument_id)
        # Note: Position closure depends on if it was a force stop or manual
        # In this implementation, we allow positions to remain unless manager forced them.
