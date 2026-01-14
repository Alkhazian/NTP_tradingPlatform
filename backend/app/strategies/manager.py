import logging
import asyncio
from typing import Dict, Optional, Type, Any
import importlib
import json
import os

from nautilus_trader.live.node import TradingNode
from .config import StrategyConfig
from .persistence import PersistenceManager
from .base import BaseStrategy

# Import known implementations 
# In a dynamic system, we might discover these automatically

logger = logging.getLogger(__name__)

class StrategyManager:
    """
    Manages the lifecycle of strategies within the NautilusTrader system.
    """
    
    def __init__(self, node: TradingNode, integration_manager=None):
        self.node = node
        self.integration_manager = integration_manager
        self.strategies: Dict[str, BaseStrategy] = {}
        self.persistence = PersistenceManager()
        self._loop = asyncio.get_event_loop()
        self._strategy_classes = {}
        self._load_registry()

    def _load_registry(self):
        """
        Load supported strategies from registry.json
        """
        registry_path = os.path.join(os.path.dirname(__file__), "registry.json")
        if not os.path.exists(registry_path):
            logger.warning(f"No strategy registry found at {registry_path}")
            return

        try:
            with open(registry_path, 'r') as f:
                registry = json.load(f)
            
            for entry in registry:
                try:
                    module = importlib.import_module(entry["module"])
                    cls = getattr(module, entry["class_name"])
                    self._strategy_classes[entry["strategy_type"]] = cls
                    logger.info(f"Registered strategy type: {entry['strategy_type']}")
                except Exception as e:
                    logger.error(f"Failed to register strategy {entry.get('strategy_type')}: {e}")
                    
        except Exception as e:
            logger.error(f"Failed to load registry: {e}")

    async def initialize(self):
        """
        Load initialized strategies from config files.
        """
        logger.info("Initializing StrategyManager...")
        configs = self.persistence.list_configs()
        logger.info(f"Found {len(configs)} configurations to restore: {list(configs.keys())}")
        for strategy_id, config_dict in configs.items():
            # Determine config type based on some field or default to StrategyConfig
            try:
                # We expect the config to have a 'strategy_type' field in parameters
                
                # Create generic StrategyConfig
                # Note: If saved config has keys that are not in StrategyConfig, 
                # they will go into 'parameters' if we loaded them carefully, 
                # but Pydantic ignores extras by default unless configured. 
                # We configured extra="allow" in StrategyConfig, so they might stay as fields
                # OR we should structure them into parameters. 
                # For migration, we assume saved configs are now just generic dicts.
                
                config = StrategyConfig(**config_dict)
                
                await self.create_strategy(config, auto_start=False)
                logger.info(f"Successfully restored strategy: {strategy_id}")
                
            except Exception as e:
                logger.error(f"Failed to restore strategy {strategy_id}: {e}", exc_info=True)
        
        logger.info("Finished StrategyManager initialization.")


    async def create_strategy(self, config: StrategyConfig, auto_start: bool = False):
        """
        Create and register a new strategy instance.
        """
        try:
            # infer class from type name
            strategy_type = config.strategy_type
            strategy_class = self._strategy_classes.get(strategy_type)
            
            if not strategy_class:
                raise ValueError(f"Unknown strategy type: {strategy_type}")
            
            logger.info(f"Creating strategy {config.id} ({strategy_type})")
            
            # Instantiate strategy
            strategy = strategy_class(
                config=config, 
                integration_manager=self.integration_manager,
                persistence_manager=self.persistence
            )
            
            # Register with Nautilus Trader
            # Strategies must be added before node.run_async() to avoid "Cannot add strategy" error
            # The enabled check in BaseStrategy.on_start() will prevent disabled strategies from running
            self.node.trader.add_strategy(strategy)
            self.strategies[config.id] = strategy
            
            # Persist the config
            self.persistence.save_config(config.id, config.dict())
            
            if auto_start:
                await self.start_strategy(config.id)
                
            return strategy
            
        except Exception as e:
            logger.error(f"Failed to create strategy {config.id}: {e}", exc_info=True)
            raise

    async def start_strategy(self, strategy_id: str):
        """
        Start a strategy instance.
        """
        if strategy_id not in self.strategies:
            logger.error(f"Strategy {strategy_id} not found")
            return

        strategy = self.strategies[strategy_id]
        
        # Nautilus strategies cannot be restarted once stopped.
        # If the strategy is in a terminal state, we must recreate it.
        # Check strategy state via name or string representation
        state_name = "UNKNOWN"
        if hasattr(strategy, "state"):
            if hasattr(strategy.state, "name"):
                state_name = strategy.state.name
            else:
                state_name = str(strategy.state)
        
        if state_name in ("STOPPED", "FINISHED") or "5" in state_name:
            logger.info(f"Strategy {strategy_id} is in terminal state ({state_name}). Resetting to READY.")
            try:
                strategy.reset()
                state_name = "READY" # Update local state name after reset
            except Exception as e:
                logger.error(f"Failed to reset strategy {strategy_id}: {e}")
                # Fallback to recreation ONLY if reset fails
                logger.warning(f"Reset failed for {strategy_id}, falling back to recreation.")
                config = strategy.strategy_config
                strategy = await self.create_strategy(config, auto_start=False)
                state_name = "READY"

        # Check if we should call start()
        # strategy.is_running usually means state == RUNNING
        # We also want to avoid starting if it's already STARTING
        can_start = False
        if state_name == "READY" or state_name == "INITIALIZED":
            can_start = True
        elif state_name == "STARTING" or state_name == "RUNNING":
            logger.info(f"Strategy {strategy_id} is already {state_name}, skipping start.")
            can_start = False
        else:
            # For other states, it depends, but let's try to be safe
            can_start = not strategy.is_running and state_name not in ("STOPPING", "STOPPED", "RESETTING")

        if can_start:
            # Update config enabled state BEFORE starting (handle immutable msgspec structs)
            # This ensures on_start() sees enabled=True and doesn't immediately stop
            try:
                strategy.strategy_config.enabled = True
            except (TypeError, AttributeError):
                try:
                    import msgspec
                    strategy.strategy_config = msgspec.structs.replace(strategy.strategy_config, enabled=True)
                except Exception as e:
                    logger.warning(f"Failed to update config enabled state (immutable): {e}")
            
            if self.node.is_running():
                # If strategy was stopped (e.g., because it was disabled at startup),
                # we need to reset it before starting
                if state_name == "STOPPED":
                    logger.info(f"Strategy {strategy_id} is STOPPED, resetting before start...")
                    strategy.reset()
                
                logger.info(f"Starting strategy {strategy_id} (State: {state_name})")
                strategy.start() # Nautilus Strategy start method
            else:
                logger.info(f"Node not running. Strategy {strategy_id} will start when node starts.")

            self.persistence.save_config(strategy_id, strategy.strategy_config.dict())

    async def stop_strategy(self, strategy_id: str):
        """
        Stop a strategy instance.
        """
        if strategy_id not in self.strategies:
            return

        strategy = self.strategies[strategy_id]
        if strategy.is_running:
            logger.info(f"Stopping strategy {strategy_id}")
            strategy.stop() # Nautilus Strategy stop method
            
            # Update config enabled state
            try:
                strategy.strategy_config.enabled = False
            except (TypeError, AttributeError):
                try:
                    import msgspec
                    strategy.strategy_config = msgspec.structs.replace(strategy.strategy_config, enabled=False)
                except Exception as e:
                    logger.warning(f"Failed to update config enabled state (immutable): {e}")

            self.persistence.save_config(strategy_id, strategy.strategy_config.dict())

    async def get_strategy_status(self, strategy_id: str) -> Dict[str, Any]:
        """
        Get status of a specific strategy including persistent performance metrics.
        """
        if strategy_id not in self.strategies:
            return {}
            
        strategy = self.strategies[strategy_id]
        
        # Default metrics
        metrics = {
            "total_trades": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0 # Renamed from realized_pnl to match DB
        }
        
        # Fetch from TradeRecorder if available
        if self.integration_manager:
            recorder = getattr(self.integration_manager, 'trade_recorder', None)
            if recorder:
                try:
                    stats = await recorder.get_strategy_stats(strategy_id)
                    metrics.update(stats)
                except Exception as e:
                    logger.error(f"Failed to fetch stats for {strategy_id}: {e}")

        # Determine display status
        display_running = strategy.is_running
        if display_running and hasattr(strategy, "_functional_ready") and not strategy._functional_ready:
            # Technically running as an actor, but functional logic is still waiting (e.g. for instruments)
            status_text = "INITIALIZING"
        elif display_running:
            status_text = "RUNNING"
        else:
            status_text = "STOPPED"

        return {
            "id": strategy_id,
            "running": display_running,
            "status": status_text, 
            "config": strategy.strategy_config.dict(),
            "state": strategy.get_state() if hasattr(strategy, "get_state") else {},
            "metrics": metrics
        }

    async def get_all_strategies_status(self) -> list:
        # Fetch all statuses concurrently
        tasks = [self.get_strategy_status(sid) for sid in self.strategies]
        return await asyncio.gather(*tasks)

    async def stop_all_strategies(self):
        """
        Gracefully stop all strategies.
        """
        for sid in list(self.strategies.keys()):
            await self.stop_strategy(sid)

    async def update_strategy_config(self, strategy_id: str, new_config_dict: dict) -> bool:
        """
        Update the full configuration of an existing strategy.
        Returns True if successful.
        """
        if strategy_id not in self.strategies:
            return False
            
        strategy = self.strategies[strategy_id]
        
        try:
            # If the dict is just parameters (old UI), wrap it
            if "id" not in new_config_dict and "strategy_type" not in new_config_dict:
                # Legacy support for existing frontend before I update it
                current_config = strategy.strategy_config.dict()
                # Update top level fields if they exist in the update
                for key in ["order_size", "instrument_id", "enabled", "name", "strategy_type"]:
                    if key in new_config_dict:
                        current_config[key] = new_config_dict[key]
                
                # Update parameters
                if "parameters" in new_config_dict:
                    current_config["parameters"].update(new_config_dict["parameters"])
                else:
                    # Flat parameters from old UI
                    for key, val in new_config_dict.items():
                        if key not in ["order_size", "instrument_id", "enabled", "name", "strategy_type"]:
                            current_config["parameters"][key] = val
                
                new_config_dict = current_config

            # Validate with StrategyConfig model
            validated_config = StrategyConfig(**new_config_dict)
            
            # Ensure ID hasn't changed (or handle rename if we wanted to, but let's stick to update)
            if validated_config.id != strategy_id:
                logger.warning(f"Config ID mismatch: {validated_config.id} vs {strategy_id}. Ignoring ID change.")
                validated_config.id = strategy_id

            # Apply to strategy
            strategy.strategy_config = validated_config
            
            # Persist change
            self.persistence.save_config(strategy_id, validated_config.dict())
            logger.info(f"Updated full configuration for strategy {strategy_id}")
            
            return True
        except Exception as e:
            logger.error(f"Failed to update config for {strategy_id}: {e}")
            raise
