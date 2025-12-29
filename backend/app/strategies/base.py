import json
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.config import StrategyConfig

class BaseStrategyConfig(StrategyConfig):
    """Base configuration for all strategies."""
    pass

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
        
    def set_redis(self, redis_conn):
        """Inject redis connection for persistence."""
        self.redis = redis_conn

    async def save_state(self):
        """Saves self.state to Redis."""
        if not self.redis:
            return
        try:
            key = f"strategy:{self.__class__.__name__}:state"
            await self.redis.set(key, json.dumps(self.user_state))
            # self.log.info(f"State saved for {self.__class__.__name__}") 
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
                self.log.info(f"State restored for {self.__class__.__name__}: {self.user_state}")
        except Exception as e:
            self.log.error(f"Failed to load state: {e}")

    def on_start(self):
        """
        Lifecycle method called when the strategy is started.
        Override this in subclasses.
        """
        self.log.info(f"Strategy {self.__class__.__name__} started.")

    def on_stop(self):
        """
        Lifecycle method called when the strategy is stopped.
        Override this in subclasses.
        """
        self.log.info(f"Strategy {self.__class__.__name__} stopped.")
