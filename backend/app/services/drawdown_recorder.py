"""
Drawdown Recorder Service

Records maximum drawdown statistics for each trade.
Data is stored in trade_drawdowns.db alongside trades.db.
Each record contains: trade date, entry time, exit time, max drawdown, final result.
"""

import sqlite3
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger("app.services.drawdown_recorder")


class DrawdownRecorder:
    """
    Records maximum drawdown per trade to a simple SQLite database.
    File format: One row per trade with:
    - trade_date: Date of the trade (YYYY-MM-DD)
    - entry_time: Time position was opened (HH:MM:SS)
    - exit_time: Time position was closed (HH:MM:SS)
    - max_drawdown: Maximum negative P&L reached during the trade (in dollars)
    - final_result: Final P&L when trade was closed (in dollars)
    - short_strike: Strike price of the short leg (sold option)
    - long_strike: Strike price of the long leg (protection option)
    - entry_premium: Premium received at entry (in dollars per spread)
    """
    
    def __init__(self, db_path: str = "data/trade_drawdowns.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
        
        # Active trade tracking (one trade at a time for this strategy)
        self._current_trade_date: Optional[str] = None
        self._current_entry_time: Optional[str] = None
        self._max_drawdown: float = 0.0  # Track minimum P&L (most negative)
        self._is_tracking: bool = False
        
        # New fields for strike and premium tracking
        self._short_strike: Optional[float] = None
        self._long_strike: Optional[float] = None
        self._entry_premium: Optional[float] = None
    
    def _init_db(self):
        """Initialize the database with the drawdowns table."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_drawdowns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT,
                    max_drawdown REAL NOT NULL,
                    final_result REAL,
                    strategy_id TEXT DEFAULT 'SPX_15Min_Range'
                )
            """)
            
            # Add new columns if they don't exist (backwards compatible migration)
            self._add_column_if_not_exists(cursor, "trade_drawdowns", "short_strike", "REAL")
            self._add_column_if_not_exists(cursor, "trade_drawdowns", "long_strike", "REAL")
            self._add_column_if_not_exists(cursor, "trade_drawdowns", "entry_premium", "REAL")
            
            conn.commit()
            conn.close()
            logger.info(f"Drawdown database initialized at {self.db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize drawdown database: {e}")
    
    def _add_column_if_not_exists(self, cursor, table: str, column: str, column_type: str):
        """Safely add a column to a table if it doesn't already exist."""
        try:
            # Check if column exists using PRAGMA table_info
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [row[1] for row in cursor.fetchall()]
            
            if column not in columns:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
                logger.info(f"Added column '{column}' to table '{table}'")
        except Exception as e:
            # Log but don't fail - column might already exist
            logger.warning(f"Could not add column '{column}' to '{table}': {e}")
    
    def start_tracking(
        self, 
        trade_date: str, 
        entry_time: str,
        short_strike: Optional[float] = None,
        long_strike: Optional[float] = None,
        entry_premium: Optional[float] = None
    ):
        """
        Start tracking drawdown for a new trade.
        
        Args:
            trade_date: Trade date in YYYY-MM-DD format
            entry_time: Entry time in HH:MM:SS format
            short_strike: Strike price of the short leg (optional)
            long_strike: Strike price of the long leg (optional)
            entry_premium: Premium received at entry in dollars per spread (optional)
        """
        self._current_trade_date = trade_date
        self._current_entry_time = entry_time
        self._max_drawdown = 0.0  # Reset to 0 (no drawdown yet)
        self._is_tracking = True
        
        # Store optional strike and premium info (safely handle None)
        self._short_strike = short_strike
        self._long_strike = long_strike
        self._entry_premium = entry_premium
        
        logger.info(
            f"Started drawdown tracking | Date: {trade_date} | Entry: {entry_time} | "
            f"Short: {short_strike} | Long: {long_strike} | Premium: ${entry_premium:.2f}" if entry_premium else
            f"Started drawdown tracking | Date: {trade_date} | Entry: {entry_time}"
        )
    
    def update_drawdown(self, current_pnl: float):
        """
        Update the maximum drawdown if current P&L is worse (more negative).
        
        Args:
            current_pnl: Current unrealized P&L in dollars
        """
        if not self._is_tracking:
            return
        
        # Drawdown is the most negative P&L we've seen
        # If current P&L is more negative, update max drawdown
        if current_pnl < self._max_drawdown:
            self._max_drawdown = current_pnl
            logger.debug(f"Updated max drawdown: ${self._max_drawdown:.2f}")
    
    def finish_tracking(self, exit_time: str, final_result: float, strategy_id: str = "SPX_15Min_Range"):
        """
        Finish tracking and save the trade drawdown record to the database.
        
        Args:
            exit_time: Exit time in HH:MM:SS format
            final_result: Final P&L when trade was closed (in dollars)
            strategy_id: Strategy identifier
        """
        if not self._is_tracking:
            logger.warning("finish_tracking called but not currently tracking")
            return
        
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO trade_drawdowns 
                (trade_date, entry_time, exit_time, max_drawdown, final_result, strategy_id,
                 short_strike, long_strike, entry_premium)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                self._current_trade_date,
                self._current_entry_time,
                exit_time,
                self._max_drawdown,
                final_result,
                strategy_id,
                self._short_strike,
                self._long_strike,
                self._entry_premium
            ))
            
            conn.commit()
            conn.close()
            
            # Build log message with optional strike/premium info
            log_msg = (
                f"Saved drawdown record | Date: {self._current_trade_date} | "
                f"Entry: {self._current_entry_time} | Exit: {exit_time} | "
                f"Max Drawdown: ${self._max_drawdown:.2f} | Final: ${final_result:.2f}"
            )
            if self._short_strike is not None:
                log_msg += f" | Short: {self._short_strike}"
            if self._long_strike is not None:
                log_msg += f" | Long: {self._long_strike}"
            if self._entry_premium is not None:
                log_msg += f" | Premium: ${self._entry_premium:.2f}"
            logger.info(log_msg)
            
        except Exception as e:
            logger.error(f"Failed to save drawdown record: {e}")
        finally:
            # Reset tracking state
            self._is_tracking = False
            self._current_trade_date = None
            self._current_entry_time = None
            self._max_drawdown = 0.0
            self._short_strike = None
            self._long_strike = None
            self._entry_premium = None
    
    def get_current_max_drawdown(self) -> float:
        """Get the current maximum drawdown for the active trade."""
        return self._max_drawdown if self._is_tracking else 0.0
    
    def is_tracking(self) -> bool:
        """Check if currently tracking a trade."""
        return self._is_tracking
    
    def cancel_tracking(self):
        """Cancel tracking without saving (e.g., if entry was cancelled)."""
        if self._is_tracking:
            logger.info("Drawdown tracking cancelled")
        self._is_tracking = False
        self._current_trade_date = None
        self._current_entry_time = None
        self._max_drawdown = 0.0
        self._short_strike = None
        self._long_strike = None
        self._entry_premium = None
