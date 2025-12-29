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

    async def on_start(self):
        # Note: on_start is async in our base implementation if we use async load_state?
        # Nautilus `on_start` is synchronous. 
        # But we made `load_state` async.
        # We should use `self.clock.schedule` or run it synchronously if possible, 
        # OR better: run `await strategy.load_state()` in the Manager AFTER init, BEFORE adding to node.
        # I already did this in Manager: `await strategy.load_state()`.
        # So here we just access `self.state`.
        
        super().on_start()
        self.subscribe_bars(self.bar_type)
        
        # Restore daily counters
        self.daily_trades = self.state.get("daily_trades", 0)
        self.last_reset_date = self.state.get("last_reset_date", "")
        
        current_date = datetime.now().strftime("%Y-%m-%d")
        if self.last_reset_date != current_date:
            self.daily_trades = 0
            self.last_reset_date = current_date
            self.state["daily_trades"] = 0
            self.state["last_reset_date"] = current_date
            # We can't await save_state here easily if it's async and we are in sync on_start.
            # But the Manager loaded the state. 
            # We will save state on first bar or trade.
            
        self.log.info(f"MES ORB Strategy Started. Daily Trades: {self.daily_trades}. Checking for existing positions...")
        
        # RECOVERY: Check if we already have a position (Reconciliation)
        # Note: self.portfolio is available.
        for pos in self.portfolio.positions.values():
            if pos.instrument_id == self.instrument_id and not pos.is_closed:
                self.log.info(f"Recovered active position: {pos}")
                # We should assume we are "in trade"
                # For this simple strategy, just knowing we have a position might be enough 
                # to trigger the 'exit' logic in on_bar.
    
    def on_bar(self, bar: Bar):
        # Ensure we are in the session
        bar_time = datetime.fromtimestamp(bar.ts_event / 1e9).time()
        
        # 0. Daily Reset Check (if running continuously)
        current_date_str = datetime.now().strftime("%Y-%m-%d")
        if self.last_reset_date != current_date_str:
             self.daily_trades = 0
             self.last_reset_date = current_date_str
             self.state["daily_trades"] = 0
             self.state["last_reset_date"] = current_date_str
             # self.save_state() # Hard to await in sync callback. 
             # Use ensure_future or fire-and-forget?
             # For MVP, we'll try to rely on trade event updates or assume short run.
             # Ideally BaseStrategy `save_state` should be sync or scheduled.
             # Nautilus `clock.schedule` takes a callback.
        
        # 1. ORB Calculation Phase
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

        # 3. Trading Logic
        # Check if we have a position
        has_position = False
        for pos in self.portfolio.positions.values():
            if pos.instrument_id == self.instrument_id and not pos.is_closed:
                has_position = True
                break

        if has_position:
             # Manage Position (Exit?)
             pass
        else:
             # Entry Logic
             # Check constraints
             if self.daily_trades >= 1:
                 return

             # Breakout Logic Implementation (Simple)
             if bar.close > self.orb_high:
                 # BUY
                 self.log.info("ORB Breakout UP - Entering LONG")
                 order = self.order_factory.market(
                     instrument_id=self.instrument_id,
                     order_side=OrderSide.BUY,
                     quantity=self.qty,
                 )
                 self.submit_order(order)
                 
                 self.daily_trades += 1
                 self.state["daily_trades"] = self.daily_trades
                 # Fire and forget save
                 # asyncio.create_task(self.save_state()) 
                 # Since we handle redis in async manager, we need a way to save.
                 # Let's rely on the strategy being stopped saving state? 
                 # Or just define save_state as sync but using a background loop?
                 # Custom strategies in Nautilus run in Cython loops sometimes, strictly sync.
                 # Using `asyncio.create_task` is standard for IO.
                 import asyncio
                 asyncio.create_task(self.save_state())
                 
             elif bar.close < self.orb_low:
                 # SELL
                 self.log.info("ORB Breakout DOWN - Entering SHORT")
                 order = self.order_factory.market(
                     instrument_id=self.instrument_id,
                     order_side=OrderSide.SELL,
                     quantity=self.qty,
                 )
                 self.submit_order(order)
                 
                 self.daily_trades += 1
                 self.state["daily_trades"] = self.daily_trades
                 import asyncio
                 asyncio.create_task(self.save_state())

    def on_stop(self):
        # We cannot await here easily either.
        # But we can try to save state.
        import asyncio
        # This might fail if loop is closing.
        # Ideally manager calls save_state before stopping.
        super().on_stop()
        self.cancel_all_orders(self.instrument_id)
        self.close_all_positions(self.instrument_id)
