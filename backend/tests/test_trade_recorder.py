import pytest
import sqlite3
import pytest_asyncio
from app.services.trade_recorder import TradeRecorder

@pytest.mark.asyncio
class TestTradeRecorder:
    
    @pytest.fixture
    def recorder(self, tmp_path):
        """Fixture for TradeRecorder using temp file DB."""
        db_file = tmp_path / "test_trades.db"
        recorder = TradeRecorder(db_path=str(db_file))
        return recorder

    async def test_lifecycle_and_stats(self, recorder):
        """Test full trade lifecycle and stats calculation."""
        
        # 1. Start a WIN trade
        trade_id_1 = await recorder.start_trade(
            strategy_id="STRAT1",
            instrument_id="SPX",
            entry_time="2023-01-01T10:00:00",
            entry_price=10.0,
            quantity=1,
            direction="LONG",
            commission=1.0
        )
        assert trade_id_1 is not None
        
        # Close as WIN
        await recorder.close_trade(
            trade_id=trade_id_1,
            exit_time="2023-01-01T11:00:00",
            exit_price=15.0,
            exit_reason="TP",
            pnl=500.0,
            commission=1.0
        )
        
        # 2. Start a LOSS trade
        trade_id_2 = await recorder.start_trade(
            strategy_id="STRAT1",
            instrument_id="SPX",
            entry_time="2023-01-01T12:00:00",
            entry_price=10.0,
            quantity=1,
            direction="LONG",
            commission=1.0
        )
        
        # Close as LOSS
        await recorder.close_trade(
            trade_id=trade_id_2,
            exit_time="2023-01-01T13:00:00",
            exit_price=5.0,
            exit_reason="SL",
            pnl=-500.0,
            commission=1.0
        )
        
        # 3. Verify Stats
        stats = await recorder.get_strategy_stats("STRAT1")
        
        assert stats["total_trades"] == 2
        assert stats["win_rate"] == 50.0
        assert stats["total_pnl"] == 0.0 # 500 - 500
        assert stats["total_commission"] == 4.0 # 1+1 + 1+1
        assert stats["max_win"] == 498.0 # 500 - 2 comm
        assert stats["max_loss"] == -502.0 # -500 - 2 comm

    async def test_get_trades(self, recorder):
        """Test retrieving trades."""
        # Open and CLOSE first trade (to satisfying unique constraint)
        t1 = await recorder.start_trade("STRAT_A", "SPX", "2023-01-01T10:00", 10.0, 1, "LONG")
        await recorder.close_trade(t1, "2023-01-01T10:30", 11.0, "TP", 100.0)
        
        # Open second trade
        await recorder.start_trade("STRAT_A", "SPX", "2023-01-01T11:00", 10.0, 1, "LONG")
        
        # Open trade for different strategy (allowed concurrent)
        await recorder.start_trade("STRAT_B", "SPX", "2023-01-01T12:00", 10.0, 1, "LONG")
        
        trades_a = await recorder.get_trades_for_strategy("STRAT_A")
        assert len(trades_a) == 2
        
        trades_b = await recorder.get_trades_for_strategy("STRAT_B")
        assert len(trades_b) == 1
