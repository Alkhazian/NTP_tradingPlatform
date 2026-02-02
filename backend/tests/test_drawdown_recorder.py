import pytest
import sqlite3
from unittest.mock import MagicMock, patch
from app.services.drawdown_recorder import DrawdownRecorder

class TestDrawdownRecorder:
    
    @pytest.fixture
    def recorder(self):
        """Fixture for DrawdownRecorder using in-memory SQLite."""
        # Use in-memory database for testing
        recorder = DrawdownRecorder(db_path=":memory:")
        return recorder

    # Removed buggy test_initialization that fails with :memory: path
    # test_init_creates_table covers DB initialization correctly via fixture

    # RE-THINKING: Using :memory: with connect/close pattern CLEARS data.
    # We should use a named temporary file or shared memory URI.
    # "file::memory:?cache=shared" might work with proper URI handling, 
    # but temp file is safer for this pattern.

    @pytest.fixture
    def temp_db_recorder(self, tmp_path):
        """Fixture using a temporary file DB."""
        db_file = tmp_path / "test_drawdowns.db"
        recorder = DrawdownRecorder(db_path=str(db_file))
        return recorder

    def test_init_creates_table(self, temp_db_recorder):
        """Test table creation."""
        conn = sqlite3.connect(temp_db_recorder.db_path)
        cursor = conn.cursor()
        
        # Check table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trade_drawdowns'")
        assert cursor.fetchone() is not None
        
        # Check columns
        cursor.execute("PRAGMA table_info(trade_drawdowns)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "short_strike" in columns
        assert "max_drawdown" in columns
        conn.close()

    def test_state_tracking_logic(self, temp_db_recorder):
        """Test start_tracking, update_drawdown, logic."""
        recorder = temp_db_recorder
        
        # Not tracking initially
        assert not recorder.is_tracking()
        
        # Start tracking
        recorder.start_tracking(
            trade_date="2023-01-01", 
            entry_time="10:00:00", 
            short_strike=4000.0,
            entry_premium=2.50
        )
        assert recorder.is_tracking()
        assert recorder.get_current_max_drawdown() == 0.0
        
        # Update with positive PnL (no drawdown)
        recorder.update_drawdown(100.0)
        assert recorder.get_current_max_drawdown() == 0.0
        
        # Update with negative PnL (drawdown)
        recorder.update_drawdown(-50.0)
        assert recorder.get_current_max_drawdown() == -50.0
        
        # Update with worse PnL
        recorder.update_drawdown(-150.0)
        assert recorder.get_current_max_drawdown() == -150.0
        
        # Update with better negative (no change to max)
        recorder.update_drawdown(-100.0)
        assert recorder.get_current_max_drawdown() == -150.0

    def test_finish_tracking_persists_data(self, temp_db_recorder):
        """Test completion acts saves row."""
        recorder = temp_db_recorder
        
        recorder.start_tracking("2023-01-01", "10:00:00")
        recorder.update_drawdown(-75.0)
        
        recorder.finish_tracking(exit_time="11:00:00", final_result=25.0)
        
        assert not recorder.is_tracking()
        
        # Verify DB content
        conn = sqlite3.connect(recorder.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trade_drawdowns")
        row = cursor.fetchone()
        conn.close()
        
        # ID, Date, Entry, Exit, MaxDD, Final, Strategy, Short, Long, Premium
        # Row structure depends on schema order, but using dict factory is cleaner ideally.
        # Here we just verify values.
        assert row[1] == "2023-01-01" # trade_date
        assert row[4] == -75.0        # max_drawdown
        assert row[5] == 25.0         # final_result
