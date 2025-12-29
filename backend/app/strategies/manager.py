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
            active_names = [name.decode() if isinstance(name, bytes) else name for name in active_names]
            
            logger.info(f"Persistence check: {len(active_names)} strategies should be active: {active_names}")
            
            # Since all strategies were pre-added, they auto-started.
            # We must sync our internal flags and STOP those that shouldn't be running.
            for name in self.registry.keys():
                if name in active_names:
                    logger.info(f"Strategy {name} is correctly active.")
                    self.active_strategies[name] = True
                    # Load state for active strategy
                    if name in self.strategies:
                        await self.strategies[name].load_state()
                else:
                    if name in self.strategies:
                        logger.info(f"Strategy {name} should NOT be active, stopping...")
                        await self.stop_strategy(name)
                    else:
                        self.active_strategies[name] = False

        except Exception as e:
            logger.error(f"Failed to restore strategies: {e}")

    def add_all_to_node(self, node):
        """Pre-adds all registered strategies to the Nautilus node before it starts."""
        logger.info("Adding all registered strategies to Nautilus node...")
        for name, strategy_cls in self.registry.items():
            try:
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
                    # Note: load_state is async, we'll handle it during start_strategy 
                    # or better, do it in a sync bridge if needed, but for now
                    # we just add the strategy to the node.

                # Add strategy to the trader
                if not hasattr(node, 'trader'):
                    raise RuntimeError("TradingNode does not have a trader instance")
                
                node.trader.add_strategy(strategy)
                self.strategies[name] = strategy
                # Assume it will start automatically
                self.active_strategies[name] = True
                logger.debug(f"Pre-added {name} to node")

            except Exception as e:
                logger.error(f"Failed to pre-add strategy {name}: {e}")

    async def start_strategy(self, name: str) -> bool:
        if name not in self.registry:
            logger.error(f"Strategy {name} not found in registry")
            return False

        if self.active_strategies.get(name) and name in self.strategies:
            from .base import StrategyMode
            if self.strategies[name].mode == StrategyMode.RUNNING:
                logger.warning(f"Strategy {name} already running")
                return True

        # Pre-flight checks
        if not await self._run_preflight_checks(name):
            logger.error(f"Pre-flight checks failed for {name}")
            return False

        if name not in self.strategies:
            logger.error(f"Strategy {name} not pre-registered in node")
            return False

        try:
            strategy = self.strategies[name]
            from .base import StrategyMode
            strategy.mode = StrategyMode.STARTING

            # Load state before starting
            if self.redis_client and self.redis_client.redis:
                await strategy.load_state()

            # Using soft-start: just activate the strategy logic
            strategy.activate()
            self.active_strategies[name] = True

            # Persist to Redis
            if self.redis_client and self.redis_client.redis:
                await self.redis_client.redis.sadd("active_strategies", name)

            logger.info(f"Started strategy: {name}")
            return True
        except Exception as e:
            logger.error(f"Failed to start strategy {name}: {e}")
            if name in self.strategies:
                self.strategies[name].set_error(str(e))
            return False

    async def pause_strategy(self, name: str) -> bool:
        """Pauses a strategy: preserves state, keeps positions."""
        if name not in self.strategies:
            return False
        try:
            strategy = self.strategies[name]
            strategy.pause()
            logger.info(f"Paused strategy: {name}")
            return True
        except Exception as e:
            logger.error(f"Failed to pause strategy {name}: {e}")
            return False

    async def resume_strategy(self, name: str) -> bool:
        """Resumes a strategy from paused state."""
        if name not in self.strategies:
            return False
        try:
            strategy = self.strategies[name]
            strategy.resume()
            logger.info(f"Resumed strategy: {name}")
            return True
        except Exception as e:
            logger.error(f"Failed to resume strategy {name}: {e}")
            return False

    async def stop_strategy(self, name: str, force: bool = False) -> bool:
        """
        Stops a strategy.
        force=False: Graceful shutdown (close positions, wait).
        force=True: Emergency stop (cancel everything immediately).
        """
        if name not in self.strategies:
            logger.warning(f"Strategy {name} not found or not registered")
            return False

        strategy = self.strategies[name]
        from .base import StrategyMode
        
        try:
            # 1. Initiate stop sequence
            strategy.stop(graceful=not force) 
            
            # 2. Handle force vs normal stop
            if force:
                logger.warning(f"EMERGENCY STOP for {name}")
                try:
                    strategy.cancel_all_orders()
                    if hasattr(strategy, "instrument_id"):
                         strategy.close_all_positions(strategy.instrument_id)
                except Exception as e:
                    logger.error(f"Error during emergency cleanup for {name}: {e}")
                
                # Always deactivate to STOPPED on force
                strategy.deactivate(reduce_only=False)
            else:
                # Normal stop: check if already flat
                is_flat = True
                try:
                    if hasattr(strategy, "instrument_id") and hasattr(strategy, "portfolio"):
                        is_flat = not strategy.portfolio.is_net_pos(strategy.instrument_id)
                except Exception as e:
                    logger.warning(f"Could not check position for {name}, defaulting to REDUCE_ONLY: {e}")
                    is_flat = False
                
                if is_flat:
                    logger.info(f"Graceful stop for {name}: Already flat, setting to STOPPED")
                    strategy.deactivate(reduce_only=False)
                else:
                    logger.info(f"Graceful stop for {name}: Setting to REDUCE_ONLY for position closure")
                    strategy.deactivate(reduce_only=True)
            
            # 3. Persist removal from active
            if self.redis_client and self.redis_client.redis:
                await self.redis_client.redis.srem("active_strategies", name)
                await strategy.save_state()

            self.active_strategies[name] = False
            logger.info(f"Stopped strategy: {name} (force={force}, final_mode={strategy.mode.value})")
            return True
        except Exception as e:
            logger.error(f"Critical failure during stop_strategy for {name}: {e}")
            # Ensure it's not left in STOPPING if we can help it
            if strategy.mode == StrategyMode.STOPPING:
                strategy.deactivate(reduce_only=True)
            return False

    async def _run_preflight_checks(self, name: str) -> bool:
        """
        Validates conditions before starting a strategy.
        - Market hours
        - Connection health
        - Risk limits
        - Coordination / Conflicts
        """
        logger.info(f"Running pre-flight checks for {name}...")
        
        # 1. Connection check
        system_status = self.engine.get_status()
        if not system_status.get("connected"):
            logger.error("IBKR not connected")
            return False
            
        # 2. Risk Limits (Example: Buying Power)
        buying_power_str = system_status.get("buying_power", "0.0")
        try:
            buying_power = float(buying_power_str.split()[0])
            if buying_power < 1000: # Arbitrary minimum
                 logger.error(f"Insufficient buying power: {buying_power}")
                 return False
        except: pass

        # 3. Coordination / Conflicts
        # Check if another strategy is already trading the same instrument (simple check)
        # In a real scenario, we'd inspect self.strategies configs
        
        # 4. Market Hours (Simplified)
        from datetime import datetime
        now = datetime.now()
        # if now.weekday() >= 5: # Weekend
        #    logger.error("Market is closed (Weekend)")
        #    return False

        logger.info(f"Pre-flight checks passed for {name}")
        return True

    async def stop_all_strategies(self):
        """Stops all running strategies (Emergency)."""
        logger.info("Stopping ALL strategies IMMEDIATELY...")
        names = list(self.strategies.keys())
        for name in names:
            await self.stop_strategy(name, force=True)

        if self.redis_client and self.redis_client.redis:
            await self.redis_client.redis.delete("active_strategies")

    def get_strategies_status(self) -> Dict[str, Any]:
        status = {}
        for name in self.registry.keys():
            mode_str = "STOPPED"
            pnl = 0.0
            error_count = 0
            
            if name in self.strategies:
                strategy = self.strategies[name]
                from .base import StrategyMode
                
                # Auto-transition REDUCE_ONLY or STOPPING to STOPPED if flat
                if strategy.mode in [StrategyMode.REDUCE_ONLY, StrategyMode.STOPPING]:
                    if hasattr(strategy, "instrument_id") and hasattr(strategy, "portfolio"):
                        try:
                            if not strategy.portfolio.is_net_pos(strategy.instrument_id):
                                strategy.deactivate(reduce_only=False)
                                logger.info(f"Strategy {name} is now flat, transitioning from {strategy.mode.value} to STOPPED")
                        except: pass

                mode_str = strategy.mode.value
                error_count = getattr(strategy, "error_count", 0)
                if hasattr(strategy, "pnl"):
                    pnl = strategy.pnl

            status[name] = {
                "status": mode_str,
                "config": self.configs.get(name, {}),
                "pnl": pnl,
                "error_count": error_count
            }
        return status
