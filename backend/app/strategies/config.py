from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, Dict, Any, List

class StrategyConfig(BaseModel):
    """Base configuration for all strategies"""
    id: str = Field(..., description="Unique identifier for the strategy instance")
    name: str = Field(..., description="Human and machine readable name of the strategy")
    enabled: bool = Field(True, description="Whether the strategy is currently enabled")
    instrument_id: str = Field(..., description="The instrument ID to trade (e.g., MESH6.CME)")
    strategy_type: str = Field(..., description="The type/class name of the strategy to instantiate")
    order_size: int = Field(1, description="Number of contracts/shares to buy/sell")
    
    # Generic parameters container
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Strategy-specific parameters")

    model_config = ConfigDict(extra="allow")
