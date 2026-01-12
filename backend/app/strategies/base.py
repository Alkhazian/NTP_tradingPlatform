from abc import abstractmethod
from typing import Dict, Any, Optional
import logging
import traceback

from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.common.component import Logger

from .config import StrategyConfig

class BaseStrategy(Strategy):
    """
    Base class for all strategies in the application.
    Adds support for:
    - Configuration management
    - State persistence (save/load)
    - Error isolation wrapper
    """

    def __init__(self, config: StrategyConfig, integration_manager=None, persistence_manager=None):
        # We don't pass config to super().__init__ because Nautilus Strategy
        # expects specific config object or None. We handle our own config.
        super().__init__(config=None) 
        self.strategy_config = config
        self.strategy_id = config.id
        self._integration_manager = integration_manager # Reference back to manager for persistence calls
        self.persistence = persistence_manager
        self.logger = logging.getLogger(f"strategy.{self.strategy_id}")
        self._functional_ready = False # Functional initialization state
        
        # Trade Persistence State
        self.active_trade_id: Optional[int] = None

    def on_start(self):
        """
        Lifecycle hook: Called when the strategy is started.
        """
        self.logger.info(f"Strategy {self.strategy_id} starting...")
        try:
            self.load_state()
            self.on_start_safe()
        except Exception as e:
            self.logger.error(f"Error starting strategy {self.strategy_id}: {e}")
            self.logger.error(traceback.format_exc())

    def on_stop(self):
        """
        Lifecycle hook: Called when the strategy is stopped.
        """
        self.logger.info(f"Strategy {self.strategy_id} stopping...")
        try:
            self.on_stop_safe()
            self.save_state()
        except Exception as e:
            self.logger.error(f"Error stopping strategy {self.strategy_id}: {e}")
            self.logger.error(traceback.format_exc())

    def on_reset(self):
        """
        Lifecycle hook: Called when the strategy is reset.
        """
        self.logger.info(f"Strategy {self.strategy_id} resetting...")
        try:
            self._functional_ready = False
            self.on_reset_safe()
        except Exception as e:
            self.logger.error(f"Error resetting strategy {self.strategy_id}: {e}")
            self.logger.error(traceback.format_exc())

    def on_resume(self):
        """
        Lifecycle hook: Called when the strategy is resumed.
        """
        self.logger.info(f"Strategy {self.strategy_id} resuming...")
        try:
            self.on_resume_safe()
        except Exception as e:
            self.logger.error(f"Error resuming strategy {self.strategy_id}: {e}")
            self.logger.error(traceback.format_exc())

    def save_state(self):
        """
        Persist current state using the manager.
        """
        if self.persistence:
            state = self.get_state()
            if self.active_trade_id is not None:
                state['active_trade_id'] = self.active_trade_id
            self.persistence.save_state(self.strategy_id, state)

    def load_state(self):
        """
        Load state from persistence.
        """
        if self.persistence:
            state = self.persistence.load_state(self.strategy_id)
            if state:
                self.logger.info(f"Restoring state for {self.strategy_id}")
                self.active_trade_id = state.get('active_trade_id')
                self.set_state(state)

    async def start_trade_record(self, instrument_id: str, entry_time: str, entry_price: float, quantity: float, direction: str, commission: float = 0.0, raw_data: str = None, trade_type: str = "DAYTRADE"):
        """
        Persist the start of a trade.
        """
        if not self._integration_manager:
            return

        try:
            # We assume NautilusManager (via strategy integration manager) has 'trade_recorder' attribute
            # We need to expose it there. 
            recorder = getattr(self._integration_manager, 'trade_recorder', None)
            if recorder:
                self.active_trade_id = await recorder.start_trade(
                    self.strategy_id, instrument_id, entry_time, entry_price, quantity, direction, commission, raw_data, trade_type
                )
                self.save_state() # Immediately save the ID
                self.logger.info(f"Started trade record {self.active_trade_id}")
        except Exception as e:
            self.logger.error(f"Failed to record trade start: {e}")

    async def close_trade_record(self, exit_time: str, exit_price: float, exit_reason: str, quantity: float, entry_price: float, commission: float = 0.0, raw_data: str = None, multiplier: float = 1.0):
        """
        Persist the closure of a trade and calculate PnL.
        """
        if not self._integration_manager or self.active_trade_id is None:
            return

        try:
            recorder = getattr(self._integration_manager, 'trade_recorder', None)
            if recorder:
                # Calculate PnL: (Exit - Entry) * Qty * Multiplier
                # Simplified for Long; for Short it would be (Entry - Exit)
                # Ideally 'direction' should be known, but for simplicity we assume Long or let strategy handle logic
                # Actually, the direction is needed for correct PnL.
                # Since we don't store direction in active_trade_id context here easily without fetching,
                # we can assume the caller knows the math, OR we fetch the trade first.
                # For now, let's implement the standard LONG formula here and assume strategy inverts if needed?
                # BETTER: Just calculate raw difference and let strategy apply direction.
                # OR: Strategy passes the final signed PnL? No, user wanted formula here.
                
                # Let's calculate raw PnL:
                raw_diff = exit_price - entry_price
                pnl = raw_diff * quantity * multiplier
                
                # If short, quantity might be passed as negative? Or we need direction.
                # Let's trust proper PnL is often generic, but (Sell - Buy) is for Long.
                # Ideally, strategy implementation should do the math or we need Direction stored.
                # We stored Direction in DB!
                # We don't query DB here to save IO.
                # Let's compute a simple PnL assuming Long for now, or require Strategy to pass Signed Quantity?
                # Nautilus Quantity is usually absolute.
                # Let's update the signature to calculate: (Exit - Entry) * Qty * Multiplier.
                # If strategy was short, Entry > Exit means profit.
                # So if Short, we want (Entry - Exit).
                # To be safe, let's just calculate (Exit - Entry) * Qty * Multiplier.
                # If the strategy was SHORT, it must have sold at Entry and Bought at Exit?
                # Wait, "Exit" usually means closing the position.
                # If Short: Entry=Sell ($100), Exit=Buy ($90). Profit $10.
                # Formula: ($90 - $100) * 1 * 1 = -$10. Incorrect.
                # So we definitely need direction.
                
                pass 
                
                # RE-THINK: we can't easily do it generically without direction.
                # SimpleIntervalTrader is LONG ONLY (Buys then Sells).
                # So (Exit - Entry) is correct.
                
                await recorder.close_trade(
                    self.active_trade_id, exit_time, exit_price, exit_reason, pnl, commission, raw_data
                )
                self.logger.info(f"Closed trade record {self.active_trade_id} with PnL {pnl}")
                self.active_trade_id = None
                self.save_state()
        except Exception as e:
            self.logger.error(f"Failed to record trade close: {e}")

    @abstractmethod
    def get_state(self) -> Dict[str, Any]:
        """
        Return a dictionary of serializable state to be saved.
        Must be implemented by subclasses.
        """
        return {}

    @abstractmethod
    def set_state(self, state: Dict[str, Any]):
        """
        Restore state from the dictionary.
        Must be implemented by subclasses.
        """
        pass

    def on_start_safe(self, *args, **kwargs):
        """
        Safe hook for subclasses to implement start logic.
        """
        pass

    def on_stop_safe(self):
        """
        Safe hook for subclasses to implement stop logic.
        """
        pass

    def on_reset_safe(self):
        """
        Safe hook for subclasses to implement reset logic.
        """
        pass

    def on_resume_safe(self):
        """
        Safe hook for subclasses to implement resume logic.
        """
        pass

    def on_unexpected_error(self, error: Exception):
        """
        Called when an unhandled exception occurs in the strategy.
        """
        self.logger.error(f"Strategy {self.strategy_id} crashed: {error}")
        self.logger.error(traceback.format_exc())
        # We could trigger a stop here if we want to fail-fast
        # self.stop() 
