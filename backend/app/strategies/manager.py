import logging
import asyncio
from typing import Dict, Any, Type
from .base import BaseStrategy, BaseStrategyConfig
from ..engine.redis_client import RedisClient

logger = logging.getLogger(__name__)


class StrategyManager:
    """Manages strategy lifecycle: registration, start, stop, and persistence."""

    def __init__(self, engine, redis_client: RedisClient):
        self.engine = engine
        self.redis_client = redis_client
        self.registry: Dict[str, Type[BaseStrategy]] = {}
        self.configs: Dict[str, Dict[str, Any]] = {}
        self.strategies: Dict[str, BaseStrategy] = {}
        self.active_strategies: Dict[str, bool] = {}

    def register_strategy(self, name: str, strategy_cls: Type[BaseStrategy], default_config: Dict[str, Any]):
        """Register a strategy class with its default configuration."""
        logger.info(f"Registering strategy: {name}")
        self.registry[name] = strategy_cls
        self.configs[name] = default_config

    async def restore_strategies(self):
        """Restores previously active strategies from Redis on startup."""
        if not self.redis_client or not self.redis_client.redis:
            logger.warning("Redis unavailable, cannot restore strategies.")
            return

        try:
            active_names = await self.redis_client.redis.smembers("active_strategies")
            for name in active_names:
                if name in self.registry:
                    logger.info(f"Restoring strategy: {name}")
                    await self.start_strategy(name)
                else:
                    logger.warning(f"Strategy {name} found in persistence but not registered.")
        except Exception as e:
            logger.error(f"Failed to restore strategies: {e}")

    async def start_strategy(self, name: str) -> bool:
        if name not in self.registry:
            logger.error(f"Strategy {name} not found in registry")
            return False

        if name in self.strategies:
            logger.warning(f"Strategy {name} already active")
            return True

        logger.info(f"Starting strategy {name}...")

        try:
            strategy_cls = self.registry[name]
            config_dict = self.configs.get(name, {})

            # Inspect the config type from the strategy's __init__ signature
            import inspect
            sig = inspect.signature(strategy_cls.__init__)
            params = list(sig.parameters.values())
            config_cls = params[1].annotation if len(params) > 1 else None

            if config_cls and config_cls is not inspect.Parameter.empty:
                config_obj = config_cls(**config_dict)
            else:
                config_obj = BaseStrategyConfig(**config_dict)

            strategy = strategy_cls(config=config_obj)

            # Inject Redis for state persistence
            if self.redis_client and self.redis_client.redis:
                strategy.set_redis(self.redis_client.redis)
                await strategy.load_state()

            # Add to Nautilus Node
            self.engine.node.add_strategy(strategy)

            # Start the strategy
            # Note: Nautilus may not have start_strategy; strategies auto-start when node runs.
            # If node is already running, we may need a different approach.
            # For now, assume node.start_strategy exists or strategies start on add.
            if hasattr(self.engine.node, 'start_strategy'):
                await self.engine.node.start_strategy(strategy)

            self.strategies[name] = strategy
            self.active_strategies[name] = True

            # Persist to Redis
            if self.redis_client and self.redis_client.redis:
                await self.redis_client.redis.sadd("active_strategies", name)

            logger.info(f"Started strategy: {name}")
            return True
        except Exception as e:
            logger.error(f"Failed to start strategy {name}: {e}")
            return False

    async def stop_strategy(self, name: str) -> bool:
        if name not in self.strategies:
            return False

        try:
            strategy = self.strategies[name]
            strategy.stop()

            # Persist removal
            if self.redis_client and self.redis_client.redis:
                await self.redis_client.redis.srem("active_strategies", name)

            del self.strategies[name]
            self.active_strategies.pop(name, None)
            logger.info(f"Stopped strategy: {name}")
            return True
        except Exception as e:
            logger.error(f"Failed to stop strategy {name}: {e}")
            return False

    async def stop_all_strategies(self):
        """Stops all running strategies."""
        logger.info("Stopping ALL strategies...")
        names = list(self.strategies.keys())
        for name in names:
            await self.stop_strategy(name)

        if self.redis_client and self.redis_client.redis:
            await self.redis_client.redis.delete("active_strategies")

    def get_strategies_status(self) -> Dict[str, Any]:
        status = {}
        for name in self.registry.keys():
            is_active = name in self.strategies
            s_status = "RUNNING" if is_active else "STOPPED"

            pnl = 0.0
            if is_active and hasattr(self.strategies[name], "pnl"):
                pnl = self.strategies[name].pnl

            status[name] = {
                "status": s_status,
                "config": self.configs.get(name, {}),
                "pnl": pnl
            }
        return status
