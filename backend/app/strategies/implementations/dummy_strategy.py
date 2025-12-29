from app.strategies.base import BaseStrategy, BaseStrategyConfig

class DummyStrategyConfig(BaseStrategyConfig):
    param1: str = "value1"
    stop_loss: float = 100.0

class DummyStrategy(BaseStrategy):
    def __init__(self, config: DummyStrategyConfig):
        super().__init__(config)
        self.param1 = config.param1

    def on_start(self):
        super().on_start()
        self.log.info(f"Dummy Strategy initialized with param1={self.param1}")

    def on_bar(self, bar):
        from ..base import StrategyMode
        if self.mode == StrategyMode.INACTIVE:
            return
        if self.mode == StrategyMode.REDUCE_ONLY:
            return # Block entries
        # Dummy logic would go here

    def on_stop(self):
        super().on_stop()

