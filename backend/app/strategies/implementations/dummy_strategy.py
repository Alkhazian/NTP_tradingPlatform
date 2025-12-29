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
        # Schedule a heartbeat log every 5 seconds to verify it's running
        self.clock.schedule(self._log_heartbeat, interval=5.0)

    def _log_heartbeat(self):
        self.log.info("Dummy Strategy Heartbeat: Still running...")

    def on_stop(self):
        super().on_stop()

