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

    def __init__(self, config: StrategyConfig, integration_manager=None):
        # We don't pass config to super().__init__ because Nautilus Strategy
        # expects specific config object or None. We handle our own config.
        super().__init__(config=None) 
        self.strategy_config = config
        self.strategy_id = config.id
        self._integration_manager = integration_manager # Reference back to manager for persistence calls
        self.logger = logging.getLogger(f"strategy.{self.strategy_id}")

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

    def save_state(self):
        """
        Persist current state using the manager.
        """
        if self._integration_manager:
            state = self.get_state()
            self._integration_manager.persistence.save_state(self.strategy_id, state)

    def load_state(self):
        """
        Load state from persistence.
        """
        if self._integration_manager:
            state = self._integration_manager.persistence.load_state(self.strategy_id)
            if state:
                self.logger.info(f"Restoring state for {self.strategy_id}")
                self.set_state(state)

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

    def on_start_safe(self):
        """
        Safe hook for subclasses to implement start logic.
        """
        pass

    def on_stop_safe(self):
        """
        Safe hook for subclasses to implement stop logic.
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
