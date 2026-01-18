"""
Example: How to use SPXBaseStrategy

This example demonstrates how to create a simple SPX-based strategy
by inheriting from SPXBaseStrategy.
"""

from typing import Dict, Any
from nautilus_trader.model.data import QuoteTick
from app.strategies.base_spx import SPXBaseStrategy
from app.strategies.config import StrategyConfig


class SimpleSPXStrategy(SPXBaseStrategy):
    """
    Example strategy that trades based on SPX price movements.
    
    This is a minimal example showing how to:
    1. Inherit from SPXBaseStrategy
    2. Implement required abstract methods
    3. React to SPX price changes
    """
    
    def __init__(self, config: StrategyConfig, integration_manager=None, persistence_manager=None):
        super().__init__(config, integration_manager, persistence_manager)
        
        # Strategy-specific state
        self.entry_threshold = config.parameters.get("entry_threshold", 5900.0)
        self.has_entered = False
    
    # =========================================================================
    # REQUIRED ABSTRACT METHODS
    # =========================================================================
    
    def on_spx_ready(self):
        """
        Called when SPX subscription is ready.
        
        Use this to initialize strategy-specific logic that requires SPX data.
        """
        self.logger.info(f"SPX is ready! Current price: {self.current_spx_price:.2f}")
        self.logger.info(f"Strategy will enter when SPX > {self.entry_threshold}")
    
    def on_spx_tick(self, tick: QuoteTick):
        """
        Called for each SPX quote tick.
        
        self.current_spx_price is already updated before this is called.
        """
        # Example: Enter position when SPX crosses threshold
        if not self.has_entered and self.current_spx_price > self.entry_threshold:
            self.logger.info(
                f"SPX crossed threshold! Price: {self.current_spx_price:.2f} > {self.entry_threshold}"
            )
            self._try_entry()
    
    # =========================================================================
    # STRATEGY-SPECIFIC LOGIC
    # =========================================================================
    
    def _try_entry(self):
        """Example entry logic"""
        # Check if we can enter
        can_enter, reason = self.can_submit_entry_order()
        
        if not can_enter:
            self.logger.warning(f"Cannot enter: {reason}")
            return
        
        self.logger.info("Entering position based on SPX signal")
        # ... your entry logic here ...
        self.has_entered = True
    
    # =========================================================================
    # STATE PERSISTENCE (Optional - only if you have additional state)
    # =========================================================================
    
    def get_state(self) -> Dict[str, Any]:
        """Save strategy state"""
        state = super().get_state()
        state["has_entered"] = self.has_entered
        return state
    
    def set_state(self, state: Dict[str, Any]):
        """Restore strategy state"""
        super().set_state(state)
        self.has_entered = state.get("has_entered", False)
