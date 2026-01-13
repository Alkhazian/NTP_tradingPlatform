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
            self.on_unexpected_error(e)

    def on_stop(self):
        """
        Lifecycle hook: Called when the strategy is stopped.
        """
        self.logger.info(f"Strategy {self.strategy_id} stopping...")
        try:
            self.on_stop_safe()
            self.save_state()
        except Exception as e:
            self.on_unexpected_error(e)

    def on_reset(self):
        """
        Lifecycle hook: Called when the strategy is reset.
        """
        self.logger.info(f"Strategy {self.strategy_id} resetting...")
        try:
            self._functional_ready = False
            self.on_reset_safe()
        except Exception as e:
            self.on_unexpected_error(e)

    def on_resume(self):
        """
        Lifecycle hook: Called when the strategy is resumed.
        """
        self.logger.info(f"Strategy {self.strategy_id} resuming...")
        try:
            self.on_resume_safe()
        except Exception as e:
            self.on_unexpected_error(e)

    def on_order_submitted(self, event):
        try:
            self.on_order_submitted_safe(event)
        except Exception as e:
            self.on_unexpected_error(e)

    def on_order_canceled(self, event):
        try:
            self.on_order_canceled_safe(event)
        except Exception as e:
            self.on_unexpected_error(e)

    def on_order_rejected(self, event):
        try:
            self.on_order_rejected_safe(event)
        except Exception as e:
            self.on_unexpected_error(e)

    def on_order_expired(self, event):
        try:
            self.on_order_expired_safe(event)
        except Exception as e:
            self.on_unexpected_error(e)

    def on_order_filled(self, event):
        try:
            self.on_order_filled_safe(event)
        except Exception as e:
            self.on_unexpected_error(e)

    def on_bar(self, bar):
        try:
            self.on_bar_safe(bar)
        except Exception as e:
            self.on_unexpected_error(e)

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
                # For SimpleIntervalTrader (Long-only): (Exit - Entry)
                pnl = (exit_price - entry_price) * quantity * multiplier
                
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
        """
        return {}

    @abstractmethod
    def set_state(self, state: Dict[str, Any]):
        """
        Restore state from the dictionary.
        """
        pass

    # Safe hooks for subclasses
    def on_start_safe(self, *args, **kwargs): pass
    def on_stop_safe(self): pass
    def on_reset_safe(self): pass
    def on_resume_safe(self): pass
    def on_order_submitted_safe(self, event): pass
    def on_order_canceled_safe(self, event): pass
    def on_order_rejected_safe(self, event): pass
    def on_order_expired_safe(self, event): pass
    def on_order_filled_safe(self, event): pass
    def on_bar_safe(self, bar): pass

    def on_unexpected_error(self, error: Exception):
        """
        Called when an unhandled exception occurs in the strategy.
        """
        self.logger.error(f"Strategy callback failed: {error}")
        self.logger.error(traceback.format_exc())
