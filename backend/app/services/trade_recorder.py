import sqlite3
import logging
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime
import os
from functools import partial

logger = logging.getLogger("app.services.trade_recorder")

class TradeRecorder:
    def __init__(self, db_path: str = "data/trades.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Synchronous DB initialization"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    entry_time TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_time TEXT,
                    exit_price REAL,
                    exit_reason TEXT,
                    trade_type TEXT DEFAULT 'DAYTRADE',
                    quantity REAL NOT NULL,
                    direction TEXT NOT NULL, 
                    pnl REAL,
                    commission REAL DEFAULT 0.0,
                    result TEXT,
                    raw_data TEXT
                )
            """)
            
            # Migration: Add commission column if it doesn't exist
            try:
                cursor.execute("ALTER TABLE trades ADD COLUMN commission REAL DEFAULT 0.0")
                logger.info("Migrated database: added commission column")
            except sqlite3.OperationalError:
                pass

            # Migration: Add result column if it doesn't exist
            try:
                cursor.execute("ALTER TABLE trades ADD COLUMN result TEXT")
                logger.info("Migrated database: added result column")
            except sqlite3.OperationalError:
                pass
                
            conn.commit()
            conn.close()
            logger.info(f"Trade database initialized at {self.db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize trade database: {e}")

    async def _run_query(self, query: str, params: tuple = ()) -> Any:
        """Run a query in a thread pool to avoid blocking the event loop"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._execute_sync, query, params)

    def _execute_sync(self, query: str, params: tuple):
        try:
            conn = sqlite3.connect(self.db_path)
            # Enable row factory for dict-like access if needed, but for now tuple is fine
            cursor = conn.cursor()
            cursor.execute(query, params)
            
            if query.strip().upper().startswith("SELECT"):
                result = cursor.fetchall()
            else:
                conn.commit()
                result = cursor.lastrowid
                
            conn.close()
            return result
        except Exception as e:
            logger.error(f"Database error executing {query}: {e}")
            raise

    async def start_trade(self, strategy_id: str, instrument_id: str, entry_time: str, 
                          entry_price: float, quantity: float, direction: str, 
                          commission: float = 0.0, raw_data: str = None, trade_type: str = "DAYTRADE") -> int:
        """
        Record the start of a trade. Returns the new trade ID.
        entry_time should be ISO 8601 string.
        """
        query = """
            INSERT INTO trades (strategy_id, instrument_id, entry_time, entry_price, quantity, direction, commission, raw_data, trade_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        return await self._run_query(query, (strategy_id, instrument_id, entry_time, entry_price, quantity, direction, commission, raw_data, trade_type))

    async def close_trade(self, trade_id: int, exit_time: str, exit_price: float, exit_reason: str, pnl: float, commission: float = 0.0, raw_data: str = None):
        """
        Update an existing trade with exit details, additional commission, result, and raw data.
        exit_time should be ISO 8601 string.
        """
        # Calculate result based on net PnL
        # However, we need the total commission, but we only have the 'delta' here if we use UPDATE.
        # This is tricky without knowing the entry commission here.
        # Let's use a subquery or just calculate it in a way that respects the stored value.
        
        query = """
            UPDATE trades 
            SET exit_time = ?, 
                exit_price = ?, 
                exit_reason = ?, 
                pnl = ?, 
                commission = commission + ?,
                raw_data = ?,
                result = CASE WHEN (? - (commission + ?)) > 0 THEN 'WIN' 
                              WHEN (? - (commission + ?)) < 0 THEN 'LOSS' 
                              ELSE 'BREAKEVEN' END
            WHERE id = ?
        """
        await self._run_query(query, (exit_time, exit_price, exit_reason, pnl, commission, raw_data, pnl, commission, pnl, commission, trade_id))

    async def get_trades_for_strategy(self, strategy_id: str, limit: int = 100) -> List[tuple]:
        if limit > 0:
            query = "SELECT * FROM trades WHERE strategy_id = ? ORDER BY entry_time DESC LIMIT ?"
            return await self._run_query(query, (strategy_id, limit))
        else:
            query = "SELECT * FROM trades WHERE strategy_id = ? ORDER BY entry_time ASC"
            return await self._run_query(query, (strategy_id,))

    async def get_strategy_stats(self, strategy_id: str) -> Dict[str, Any]:
        """
        Calculate aggregate stats: Win Rate, Total PnL, Trade Count, Max Win, Max Loss
        """
        # We need realized trades (where exit_time IS NOT NULL)
        query = """
            SELECT 
                COUNT(*) as total_trades,
                SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins,
                SUM(pnl) as total_pnl,
                SUM(commission) as total_commission,
                MAX(pnl - commission) as max_win_net,
                MIN(pnl - commission) as max_loss_net
            FROM trades 
            WHERE strategy_id = ? AND exit_time IS NOT NULL
        """
        result = await self._run_query(query, (strategy_id,))
        if not result or not result[0]:
            return {
                "total_trades": 0, 
                "win_rate": 0.0, 
                "total_pnl": 0.0, 
                "total_commission": 0.0,
                "net_pnl": 0.0,
                "max_win": 0.0,
                "max_loss": 0.0
            }
            
        total, wins, pnl, comm, max_win, max_loss = result[0]
        total = total or 0
        wins = wins or 0
        pnl = pnl or 0.0
        comm = comm or 0.0
        max_win = max_win or 0.0
        max_loss = max_loss or 0.0
        
        win_rate = (wins / total * 100) if total > 0 else 0.0
        net_pnl = pnl - comm
        
        return {
            "total_trades": total,
            "win_rate": round(win_rate, 2),
            "total_pnl": round(pnl, 2),
            "total_commission": round(comm, 2),
            "net_pnl": round(net_pnl, 2),
            "max_win": round(max_win, 2),
            "max_loss": round(max_loss, 2)
        }
