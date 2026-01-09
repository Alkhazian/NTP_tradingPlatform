import json
import os
import logging
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

class PersistenceManager:
    """
    Manages persistence of strategy configurations and state to the local filesystem.
    """
    def __init__(self, data_dir: str = "data/strategies"):
        self.data_dir = data_dir
        self.config_dir = os.path.join(data_dir, "config")
        self.state_dir = os.path.join(data_dir, "state")
        self._ensure_directories()

    def _ensure_directories(self):
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.state_dir, exist_ok=True)

    def save_config(self, strategy_id: str, config: Dict[str, Any]):
        """Save strategy configuration to disk"""
        try:
            filepath = os.path.join(self.config_dir, f"{strategy_id}.json")
            with open(filepath, 'w') as f:
                json.dump(config, f, indent=4)
            logger.info(f"Saved configuration for strategy {strategy_id}")
        except Exception as e:
            logger.error(f"Failed to save config for strategy {strategy_id}: {e}")

    def load_config(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        """Load strategy configuration from disk"""
        try:
            filepath = os.path.join(self.config_dir, f"{strategy_id}.json")
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    return json.load(f)
            return None
        except Exception as e:
            logger.error(f"Failed to load config for strategy {strategy_id}: {e}")
            return None

    def list_configs(self) -> Dict[str, Dict[str, Any]]:
        """List all available strategy configurations"""
        configs = {}
        try:
            if not os.path.exists(self.config_dir):
                return configs
                
            for filename in os.listdir(self.config_dir):
                if filename.endswith(".json"):
                    strategy_id = filename[:-5]
                    config = self.load_config(strategy_id)
                    if config:
                        configs[strategy_id] = config
            return configs
        except Exception as e:
            logger.error(f"Failed to list configs: {e}")
            return configs

    def save_state(self, strategy_id: str, state: Dict[str, Any]):
        """Save strategy runtime state to disk"""
        try:
            filepath = os.path.join(self.state_dir, f"{strategy_id}.json")
            # accurate timestamp for debugging
            state['_last_updated'] = datetime.utcnow().isoformat()
            
            with open(filepath, 'w') as f:
                json.dump(state, f, indent=4)
            logger.debug(f"Saved state for strategy {strategy_id}")
        except Exception as e:
            logger.error(f"Failed to save state for strategy {strategy_id}: {e}")

    def load_state(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        """Load strategy runtime state from disk"""
        try:
            filepath = os.path.join(self.state_dir, f"{strategy_id}.json")
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    return json.load(f)
            return None
        except Exception as e:
            logger.error(f"Failed to load state for strategy {strategy_id}: {e}")
            return None
