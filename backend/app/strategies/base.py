import json
from enum import Enum
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.config import StrategyConfig

class BaseStrategyConfig(StrategyConfig):
    """Base configuration for all strategies."""
    pass

class StrategyMode(Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"
    REDUCE_ONLY = "REDUCE_ONLY" # Transitionary or specific limit state

class BaseStrategy(Strategy):
    """
    Base strategy class for the trading platform.
    Inherits from NautilusTrader's Strategy class.
    
    All custom strategies should inherit from this class to ensure
    compatibility with the platform's StrategyManager and UI.
    """
    def __init__(self, config: BaseStrategyConfig):
        super().__init__(config)
        self.redis = None
        self.user_state = {} # Use this generic dict to store persistable state
        self.mode = StrategyMode.STOPPED
        self.error_count = 0
        
    def activate(self):
        """Activates the strategy logic (Full trading)."""
        self.mode = StrategyMode.RUNNING
        self.log.info(f"Strategy {self.__class__.__name__} set to RUNNING.")

    def deactivate(self, reduce_only: bool = True):
        """
        Deactivates the strategy logic. 
        Defaults to REDUCE_ONLY for safety.
        """
        if reduce_only:
            self.mode = StrategyMode.REDUCE_ONLY
            self.log.info(f"Strategy {self.__class__.__name__} set to REDUCE_ONLY.")
        else:
            self.mode = StrategyMode.STOPPED
            self.log.info(f"Strategy {self.__class__.__name__} set to STOPPED.")

    def pause(self):
        """Suspends trading activity but keeps state and positions."""
        if self.mode == StrategyMode.RUNNING:
            self.mode = StrategyMode.PAUSED
            self.log.info(f"Strategy {self.__class__.__name__} PAUSED. State preserved, positions held.")

    def resume(self):
        """Resumes trading from paused state."""
        if self.mode == StrategyMode.PAUSED:
            self.mode = StrategyMode.RUNNING
            self.log.info(f"Strategy {self.__class__.__name__} RESUMED.")

    def stop(self, graceful: bool = True):
        """
        Stops the strategy.
        graceful=True: Close positions gracefully, wait for orders.
        graceful=False: Force stop, cancel everything immediately.
        """
        self.mode = StrategyMode.STOPPING
        if not graceful:
            self.log.warning(f"Strategy {self.__class__.__name__} FORCE STOPPING. Canceling all orders/positions.")
            # Implementation will be handled in manager or subclasses calling Nautilus methods
        else:
            self.log.info(f"Strategy {self.__class__.__name__} STOPPING gracefully.")
        
    def set_error(self, message: str):
        """Sets the strategy to ERROR state."""
        self.mode = StrategyMode.ERROR
        self.log.error(f"Strategy {self.__class__.__name__} entered ERROR state: {message}")
        
    def set_redis(self, redis_conn):
        """Inject redis connection for persistence."""
        self.redis = redis_conn

    async def save_state(self):
        """Saves self.state to Redis."""
        if not self.redis:
            return
        try:
            key = f"strategy:{self.__class__.__name__}:state"
            # Include mode and error_count in persistence if needed, 
            # though manager handles most of this.
            self.user_state["_mode"] = self.mode.value
            self.user_state["_error_count"] = self.error_count
            await self.redis.set(key, json.dumps(self.user_state))
        except Exception as e:
            self.log.error(f"Failed to save state: {e}")

    async def load_state(self):
        """Loads self.state from Redis."""
        if not self.redis:
            return
        try:
            key = f"strategy:{self.__class__.__name__}:state"
            data = await self.redis.get(key)
            if data:
                self.user_state = json.loads(data)
                # Restore mode if it was paused or running
                saved_mode = self.user_state.get("_mode")
                if saved_mode:
                    try:
                        restored_mode = StrategyMode(saved_mode)
                        # Don't restore transitional or error states to 'active' ones
                        if restored_mode in [StrategyMode.STARTING, StrategyMode.STOPPING]:
                            self.mode = StrategyMode.STOPPED
                        else:
                            self.mode = restored_mode
                    except ValueError: 
                        self.mode = StrategyMode.STOPPED
                self.error_count = self.user_state.get("_error_count", 0)
                self.log.info(f"State restored for {self.__class__.__name__}: mode={self.mode.value}, state={self.user_state}")
        except Exception as e:
            self.log.error(f"Failed to load state: {e}")

    def on_start(self):
        """
        Lifecycle method called when the strategy is started.
        Override this in subclasses.
        """
        self.mode = StrategyMode.RUNNING
        self.log.info(f"Strategy {self.__class__.__name__} started.")

    def on_stop(self):
        """
        Lifecycle method called when the strategy is stopped.
        Override this in subclasses.
        """
        self.mode = StrategyMode.STOPPED
        self.log.info(f"Strategy {self.__class__.__name__} stopped.")
