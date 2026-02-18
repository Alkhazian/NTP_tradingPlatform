"""
Unified Trading Data Service

Replaces both DrawdownRecorder and TradeRecorder with a single service
that manages the orders + trades schema.

Features:
- Records orders and trades in SQLite
- Tracks drawdown for stop-loss tuning
- Supports spreads and single-leg options
- Fault-tolerant: individual field failures don't crash the app
"""

import sqlite3
import logging
import os
import json
from datetime import datetime
from typing import Optional, Dict, Any, List
from contextlib import contextmanager

logger = logging.getLogger("app.services.trading_data")


class TradingDataService:
    """
    Unified service for recording trading data.
    
    Manages two tables:
    - orders: Individual order executions
    - trades: Aggregated trade lifecycle
    
    Usage:
        service = TradingDataService()
        
        # On trade entry
        trade_id = service.start_trade(
            strategy_id="spx_15min_range",
            instrument_id="SPXW260123P06895000.CBOE",
            trade_type="PUT_CREDIT_SPREAD",
            entry_price=-0.95,
            quantity=2,
            direction="LONG",
            ...
        )
        
        # On order filled
        service.record_order(
            trade_id=trade_id,
            strategy_id="spx_15min_range",
            ...
        )
        
        # During trade - update drawdown
        service.update_trade_metrics(trade_id, current_pnl=-30.0)
        
        # On exit
        service.close_trade(trade_id, exit_price=-0.45, exit_reason="TAKE_PROFIT")
    """
    
    def __init__(self, db_path: str = "data/trading.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
        
        # Active trade tracking (for drawdown updates)
        self._active_trades: Dict[str, Dict[str, Any]] = {}
    
    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            if conn:
                conn.close()
    
    def _init_db(self):
        """Initialize database schema."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                # ORDERS TABLE
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        strategy_id TEXT NOT NULL,
                        instrument_id TEXT NOT NULL,
                        exchange_order_id TEXT UNIQUE,
                        client_order_id TEXT,
                        trade_id TEXT,
                        trade_type TEXT NOT NULL,
                        trade_direction TEXT NOT NULL,
                        order_side TEXT NOT NULL,
                        order_type TEXT NOT NULL,
                        quantity REAL NOT NULL,
                        price_limit REAL,
                        status TEXT NOT NULL DEFAULT 'SUBMITTED',
                        submitted_time TEXT NOT NULL,
                        filled_time TEXT,
                        filled_quantity REAL DEFAULT 0,
                        filled_price REAL,
                        commission REAL DEFAULT 0.0,
                        raw_data TEXT,
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                """)
                
                # TRADES TABLE
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_id TEXT UNIQUE NOT NULL,
                        strategy_id TEXT NOT NULL,
                        instrument_id TEXT NOT NULL,
                        trade_type TEXT NOT NULL,
                        entry_reason TEXT,
                        entry_target_price REAL,
                        entry_stop_loss REAL,
                        strikes TEXT,
                        expiration TEXT,
                        legs TEXT,
                        strategy_config TEXT,
                        entry_time TEXT NOT NULL,
                        exit_time TEXT,
                        duration_seconds INTEGER,
                        entry_price REAL NOT NULL,
                        exit_price REAL,
                        quantity REAL NOT NULL,
                        direction TEXT NOT NULL,
                        pnl REAL,
                        commission REAL DEFAULT 0.0,
                        net_pnl REAL,
                        result TEXT,
                        max_profit REAL,
                        max_loss REAL,
                        max_unrealized_profit REAL DEFAULT 0.0,
                        max_unrealized_loss REAL DEFAULT 0.0,
                        max_unrealized_loss_time TEXT,
                        entry_premium_per_contract REAL,
                        pnl_snapshots TEXT,
                        status TEXT NOT NULL DEFAULT 'OPEN',
                        exit_reason TEXT,
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                """)
                
                # Indexes
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_strategy ON orders(strategy_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_trade_id ON orders(trade_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_filled_time ON orders(filled_time)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
                
                conn.commit()
                logger.info(f"Trading database initialized at {self.db_path}")
                
        except Exception as e:
            logger.error(f"Failed to initialize trading database: {e}")
    
    # =========================================================================
    # TRADE LIFECYCLE
    # =========================================================================
    
    def start_trade(
        self,
        trade_id: str,
        strategy_id: str,
        instrument_id: str,
        trade_type: str,
        entry_price: float,
        quantity: float,
        direction: str,
        entry_time: Optional[str] = None,
        entry_reason: Optional[Dict] = None,
        entry_target_price: Optional[float] = None,
        entry_stop_loss: Optional[float] = None,
        strikes: Optional[List[str]] = None,
        expiration: Optional[str] = None,
        legs: Optional[List[Dict]] = None,
        strategy_config: Optional[Dict] = None,
        max_profit: Optional[float] = None,
        max_loss: Optional[float] = None,
        entry_premium_per_contract: Optional[float] = None,
    ) -> Optional[str]:
        """
        Create a new trade record when entering a position.
        
        Returns:
            trade_id if successful, None if failed
        """
        try:
            entry_time = entry_time or datetime.utcnow().isoformat() + "Z"
            
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    INSERT INTO trades (
                        trade_id, strategy_id, instrument_id, trade_type,
                        entry_price, quantity, direction, entry_time,
                        entry_reason, entry_target_price, entry_stop_loss,
                        strikes, expiration, legs, strategy_config,
                        max_profit, max_loss, entry_premium_per_contract,
                        status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
                """, (
                    trade_id,
                    strategy_id,
                    instrument_id,
                    trade_type,
                    entry_price,
                    quantity,
                    direction,
                    entry_time,
                    self._safe_json(entry_reason),
                    entry_target_price,
                    entry_stop_loss,
                    self._safe_json(strikes),
                    expiration,
                    self._safe_json(legs),
                    self._safe_json(strategy_config),
                    max_profit,
                    max_loss,
                    entry_premium_per_contract,
                ))
                
                conn.commit()
            
            # Track for live drawdown updates
            self._active_trades[trade_id] = {
                "entry_stop_loss": entry_stop_loss,
                "max_unrealized_profit": 0.0,
                "max_unrealized_loss": 0.0,
                "max_dd_time": None,
                "pnl_snapshots": [],
            }
            
            logger.info(f"Trade started: {trade_id} | {trade_type} | {direction} | Entry: {entry_price}")
            return trade_id
            
        except Exception as e:
            logger.error(f"Failed to start trade {trade_id}: {e}")
            return None
    
    def update_trade_metrics(
        self,
        trade_id: str,
        current_pnl: float,
        timestamp: Optional[str] = None,
    ) -> bool:
        """
        Update trade metrics during the trade lifecycle.
        Tracks max profit, max drawdown for stop-loss tuning.
        
        Args:
            trade_id: Trade identifier
            current_pnl: Current unrealized P&L in dollars
            timestamp: Optional timestamp, defaults to now
            
        Returns:
            True if update successful
        """
        try:
            if trade_id not in self._active_trades:
                # Load from DB if not in memory
                self._load_active_trade(trade_id)
            
            trade_data = self._active_trades.get(trade_id)
            if not trade_data:
                logger.warning(f"Trade {trade_id} not found for metrics update")
                return False
            
            timestamp = timestamp or datetime.utcnow().isoformat() + "Z"
            updates_needed = False
            
            # Update max profit
            if current_pnl > trade_data["max_unrealized_profit"]:
                trade_data["max_unrealized_profit"] = current_pnl
                updates_needed = True
            
            # Update max drawdown (most negative)
            if current_pnl < trade_data["max_unrealized_loss"]:
                trade_data["max_unrealized_loss"] = current_pnl
                trade_data["max_dd_time"] = timestamp
                updates_needed = True
                logger.debug(f"Trade {trade_id} new max drawdown: ${current_pnl:.2f}")
            
            # Add P&L snapshot (every call, for curve analysis)
            trade_data["pnl_snapshots"].append({
                "ts": timestamp,
                "pnl": round(current_pnl, 2)
            })
            
            # Limit snapshots to last 1000 to prevent memory bloat
            if len(trade_data["pnl_snapshots"]) > 1000:
                trade_data["pnl_snapshots"] = trade_data["pnl_snapshots"][-1000:]
            
            # Persist to DB (throttle to every 10th update for performance)
            if updates_needed:
                self._persist_trade_metrics(trade_id, trade_data)
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to update trade metrics {trade_id}: {e}")
            return False
    
    def close_trade(
        self,
        trade_id: str,
        exit_price: float,
        exit_reason: str,
        exit_time: Optional[str] = None,
        commission: Optional[float] = None,
    ) -> bool:
        """
        Close a trade and calculate final P&L.
        
        Args:
            trade_id: Trade identifier
            exit_price: Exit fill price
            exit_reason: STOP_LOSS, TAKE_PROFIT, MANUAL, EOD, EXPIRY
            exit_time: Optional timestamp
            commission: Total commission for the trade
            
        Returns:
            True if close successful
        """
        try:
            exit_time = exit_time or datetime.utcnow().isoformat() + "Z"
            trade_data = self._active_trades.get(trade_id, {})
            
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                # Get trade entry data
                cursor.execute(
                    "SELECT entry_price, quantity, entry_time, entry_stop_loss, max_unrealized_loss_time "
                    "FROM trades WHERE trade_id = ?",
                    (trade_id,)
                )
                row = cursor.fetchone()
                
                if not row:
                    logger.error(f"Trade {trade_id} not found for closing")
                    return False
                
                entry_price = row["entry_price"]
                quantity = row["quantity"]
                entry_time = row["entry_time"]
                entry_stop_loss = row["entry_stop_loss"]
                max_dd_time = row["max_unrealized_loss_time"] or trade_data.get("max_dd_time")
                
                # Calculate P&L
                # For credit spreads: entry is negative (credit), exit is negative (debit to close)
                # P&L = (exit_price - entry_price) * multiplier * quantity
                # For credit spread LONG: entry=-0.95, exit=-0.45 → pnl = (-0.45 - (-0.95)) * 100 * 2 = +100
                pnl = (exit_price - entry_price) * 100 * quantity
                
                # Calculate duration
                try:
                    entry_dt = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
                    exit_dt = datetime.fromisoformat(exit_time.replace("Z", "+00:00"))
                    duration_seconds = int((exit_dt - entry_dt).total_seconds())
                except:
                    duration_seconds = None
                


                # Commission
                total_commission = commission or 0.0
                net_pnl = pnl - total_commission
                
                # Result
                if net_pnl > 0:
                    result = "WIN"
                elif net_pnl < 0:
                    result = "LOSS"
                else:
                    result = "BREAKEVEN"
                
                # Update trade
                cursor.execute("""
                    UPDATE trades SET
                        exit_time = ?,
                        exit_price = ?,
                        exit_reason = ?,
                        duration_seconds = ?,
                        pnl = ?,
                        commission = ?,
                        net_pnl = ?,
                        result = ?,
                        max_unrealized_profit = ?,
                        max_unrealized_loss = ?,
                        max_unrealized_loss_time = ?,
                        pnl_snapshots = ?,
                        status = 'CLOSED',
                        updated_at = datetime('now')
                    WHERE trade_id = ?
                """, (
                    exit_time,
                    exit_price,
                    exit_reason,
                    duration_seconds,
                    round(pnl, 2),
                    total_commission,
                    round(net_pnl, 2),
                    result,
                    trade_data.get("max_unrealized_profit", 0),
                    trade_data.get("max_unrealized_loss", 0),
                    max_dd_time,
                    self._safe_json(trade_data.get("pnl_snapshots", [])),
                    trade_id,
                ))
                
                conn.commit()
            
            # Remove from active tracking
            self._active_trades.pop(trade_id, None)
            
            logger.info(
                f"Trade closed: {trade_id} | {exit_reason} | P&L: ${pnl:.2f} | "
                f"Max DD: ${trade_data.get('max_unrealized_loss', 0):.2f} | Result: {result}"
            )
            return True
            
        except Exception as e:
            logger.error(f"Failed to close trade {trade_id}: {e}")
            return False
    
    # =========================================================================
    # ORDER RECORDING
    # =========================================================================
    
    def record_order(
        self,
        strategy_id: str,
        instrument_id: str,
        trade_type: str,
        trade_direction: str,
        order_side: str,
        order_type: str,
        quantity: float,
        status: str,
        submitted_time: str,
        trade_id: Optional[str] = None,
        exchange_order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        price_limit: Optional[float] = None,
        filled_time: Optional[str] = None,
        filled_quantity: Optional[float] = None,
        filled_price: Optional[float] = None,
        commission: float = 0.0,
        raw_data: Optional[Dict] = None,
    ) -> Optional[int]:
        """
        Record an order execution.
        
        Returns:
            Order ID if successful, None if failed
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    INSERT INTO orders (
                        strategy_id, instrument_id, exchange_order_id, client_order_id,
                        trade_id, trade_type, trade_direction, order_side, order_type,
                        quantity, price_limit, status, submitted_time,
                        filled_time, filled_quantity, filled_price, commission, raw_data
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    strategy_id,
                    instrument_id,
                    exchange_order_id,
                    client_order_id,
                    trade_id,
                    trade_type,
                    trade_direction,
                    order_side,
                    order_type,
                    quantity,
                    price_limit,
                    status,
                    submitted_time,
                    filled_time,
                    filled_quantity,
                    filled_price,
                    commission,
                    self._safe_json(raw_data),
                ))
                
                order_id = cursor.lastrowid
                conn.commit()
                
            logger.info(f"Order recorded: #{order_id} | {trade_direction} | {order_side} | {status}")
            return order_id
            
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed: orders.exchange_order_id" in str(e):
                logger.debug(f"Order already exists: {exchange_order_id}")
            else:
                logger.error(f"Order integrity error: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to record order: {e}")
            return None
    
    def update_order(
        self,
        exchange_order_id: str,
        status: Optional[str] = None,
        filled_time: Optional[str] = None,
        filled_quantity: Optional[float] = None,
        filled_price: Optional[float] = None,
        commission: Optional[float] = None,
    ) -> bool:
        """Update an existing order with fill details."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                # Build dynamic update
                updates = []
                params = []
                
                if status is not None:
                    updates.append("status = ?")
                    params.append(status)
                if filled_time is not None:
                    updates.append("filled_time = ?")
                    params.append(filled_time)
                if filled_quantity is not None:
                    updates.append("filled_quantity = ?")
                    params.append(filled_quantity)
                if filled_price is not None:
                    updates.append("filled_price = ?")
                    params.append(filled_price)
                if commission is not None:
                    updates.append("commission = ?")
                    params.append(commission)
                
                if not updates:
                    return True
                
                updates.append("updated_at = datetime('now')")
                params.append(exchange_order_id)
                
                cursor.execute(
                    f"UPDATE orders SET {', '.join(updates)} WHERE exchange_order_id = ?",
                    params
                )
                
                conn.commit()
                return cursor.rowcount > 0
                
        except Exception as e:
            logger.error(f"Failed to update order {exchange_order_id}: {e}")
            return False
    
    # =========================================================================
    # QUERIES
    # =========================================================================
    
    def get_open_trades(self, strategy_id: Optional[str] = None) -> List[Dict]:
        """Get all open trades, optionally filtered by strategy."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                if strategy_id:
                    cursor.execute(
                        "SELECT * FROM trades WHERE status = 'OPEN' AND strategy_id = ?",
                        (strategy_id,)
                    )
                else:
                    cursor.execute("SELECT * FROM trades WHERE status = 'OPEN'")
                
                return [dict(row) for row in cursor.fetchall()]
                
        except Exception as e:
            logger.error(f"Failed to get open trades: {e}")
            return []
    
    def get_trade(self, trade_id: str) -> Optional[Dict]:
        """Get a specific trade by ID."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"Failed to get trade {trade_id}: {e}")
            return None
    
    def get_trade_orders(self, trade_id: str) -> List[Dict]:
        """Get all orders for a trade."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM orders WHERE trade_id = ? ORDER BY filled_time",
                    (trade_id,)
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get orders for trade {trade_id}: {e}")
            return []
    
    def get_strategy_stats(self, strategy_id: str) -> Dict[str, Any]:
        """Get aggregated statistics for a strategy."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT 
                        COUNT(*) as total_trades,
                        SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins,
                        SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) as losses,
                        SUM(pnl) as total_gross_pnl,
                        SUM(net_pnl) as total_net_pnl,
                        SUM(commission) as total_commission,
                        AVG(net_pnl) as avg_net_pnl,
                        MAX(net_pnl) as best_trade,
                        MIN(net_pnl) as worst_trade,
                        AVG(max_unrealized_loss) as avg_max_drawdown,
                        MIN(max_unrealized_loss) as worst_drawdown
                    FROM trades 
                    WHERE strategy_id = ? AND status = 'CLOSED'
                """, (strategy_id,))
                
                row = cursor.fetchone()
                if not row or row["total_trades"] == 0:
                    return {"total_trades": 0, "win_rate": 0, "gross_pnl": 0.0, "net_pnl": 0.0, "total_commission": 0.0}
                
                return {
                    "total_trades": row["total_trades"],
                    "wins": row["wins"] or 0,
                    "losses": row["losses"] or 0,
                    "win_rate": round(100 * (row["wins"] or 0) / row["total_trades"], 1),
                    "gross_pnl": round(row["total_gross_pnl"] or 0, 2),
                    "net_pnl": round(row["total_net_pnl"] or 0, 2),
                    "total_pnl": round(row["total_net_pnl"] or 0, 2), # Keeping total_pnl as net_pnl for backward compat if needed, but added gross_pnl explicitly
                    "total_commission": round(row["total_commission"] or 0, 2),
                    "avg_net_pnl": round(row["avg_net_pnl"] or 0, 2),
                    "max_win": round(row["best_trade"] or 0, 2),
                    "max_loss": round(row["worst_trade"] or 0, 2),
                    "avg_max_drawdown": round(row["avg_max_drawdown"] or 0, 2),
                    "worst_drawdown": round(row["worst_drawdown"] or 0, 2),
                }
                
        except Exception as e:
            logger.error(f"Failed to get strategy stats for {strategy_id}: {e}")
            return {"total_trades": 0, "error": str(e)}
    
    def get_drawdown_analysis(self, strategy_id: str) -> List[Dict]:
        """
        Get drawdown data for stop-loss tuning analysis.
        
        Returns trades with:
        - max_unrealized_loss
        - sl_distance_at_max_dd
        - entry_stop_loss
        - result
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                cursor.execute("""
                    SELECT 
                        trade_id,
                        entry_time,
                        exit_time,
                        trade_type,
                        entry_price,
                        exit_price,
                        entry_stop_loss,
                        max_unrealized_loss,
                        max_unrealized_loss_time,
                        pnl,
                        net_pnl,
                        result,
                        strikes,
                        entry_premium_per_contract
                    FROM trades 
                    WHERE strategy_id = ? AND status = 'CLOSED'
                    ORDER BY entry_time DESC
                """, (strategy_id,))
                
                return [dict(row) for row in cursor.fetchall()]
                
        except Exception as e:
            logger.error(f"Failed to get drawdown analysis for {strategy_id}: {e}")
            return []
    
    # =========================================================================
    # HELPERS
    # =========================================================================
    
    def _safe_json(self, data: Any) -> Optional[str]:
        """Safely serialize data to JSON, returning None on failure."""
        if data is None:
            return None
        try:
            return json.dumps(data)
        except Exception:
            return None
    
    def _load_active_trade(self, trade_id: str) -> bool:
        """Load a trade into active tracking from database."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT entry_stop_loss, max_unrealized_profit, max_unrealized_loss, "
                    "max_unrealized_loss_time, pnl_snapshots FROM trades WHERE trade_id = ?",
                    (trade_id,)
                )
                row = cursor.fetchone()
                
                if row:
                    self._active_trades[trade_id] = {
                        "entry_stop_loss": row["entry_stop_loss"],
                        "max_unrealized_profit": row["max_unrealized_profit"] or 0.0,
                        "max_unrealized_loss": row["max_unrealized_loss"] or 0.0,
                        "max_dd_time": row["max_unrealized_loss_time"],
                        "pnl_snapshots": json.loads(row["pnl_snapshots"]) if row["pnl_snapshots"] else [],
                    }
                    return True
                    
            return False
        except Exception as e:
            logger.error(f"Failed to load trade {trade_id}: {e}")
            return False
    
    def _persist_trade_metrics(self, trade_id: str, trade_data: Dict) -> bool:
        """Persist trade metrics to database."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE trades SET
                        max_unrealized_profit = ?,
                        max_unrealized_loss = ?,
                        max_unrealized_loss_time = ?,
                        updated_at = datetime('now')
                    WHERE trade_id = ?
                """, (
                    trade_data.get("max_unrealized_profit", 0),
                    trade_data.get("max_unrealized_loss", 0),
                    trade_data.get("max_dd_time"),
                    trade_id,
                ))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to persist trade metrics {trade_id}: {e}")
            return False
    
    def cancel_trade(self, trade_id: str) -> bool:
        """Cancel tracking for a trade (e.g., entry cancelled)."""
        self._active_trades.pop(trade_id, None)
        logger.info(f"Trade tracking cancelled: {trade_id}")
        return True

    def delete_trade(self, trade_id: str) -> bool:
        """
        Permanently delete a trade and all its associated orders from the database.

        Used when a fill timeout fires with zero fills — the order was never
        actually executed, so the pre-recorded trade entry must be removed.

        Returns:
            True if deletion was successful (or trade didn't exist), False on error.
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                # Delete associated orders first (FK-safe order)
                cursor.execute("DELETE FROM orders WHERE trade_id = ?", (trade_id,))
                orders_deleted = cursor.rowcount
                # Delete the trade record itself
                cursor.execute("DELETE FROM trades WHERE trade_id = ?", (trade_id,))
                trades_deleted = cursor.rowcount
                conn.commit()

            # Remove from in-memory tracking
            self._active_trades.pop(trade_id, None)

            logger.info(
                f"Trade deleted from DB: {trade_id} | "
                f"trades removed: {trades_deleted}, orders removed: {orders_deleted}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to delete trade {trade_id}: {e}")
            return False

    def update_trade_quantity(self, trade_id: str, actual_quantity: float) -> bool:
        """
        Update the quantity of an existing trade and its entry order.

        Used when a fill timeout fires with a partial fill — the position exists
        but with fewer contracts than originally ordered.  Both the trade record
        and the corresponding ENTRY order row are updated so that max_profit,
        max_loss, and commission calculations remain consistent.

        Args:
            trade_id:        The trade identifier to update.
            actual_quantity: The number of contracts actually filled.

        Returns:
            True if update was successful, False on error.
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                # Fetch current trade to recalculate derived fields
                cursor.execute(
                    "SELECT entry_price, max_profit, max_loss, entry_premium_per_contract "
                    "FROM trades WHERE trade_id = ?",
                    (trade_id,),
                )
                row = cursor.fetchone()
                if not row:
                    logger.error(f"Trade {trade_id} not found for quantity update")
                    return False

                entry_price = row["entry_price"]
                entry_premium = row["entry_premium_per_contract"]

                # Recalculate max_profit / max_loss proportionally
                new_max_profit = (entry_premium * actual_quantity) if entry_premium else None
                # max_loss stored as positive dollar amount per original quantity
                old_max_loss = row["max_loss"]
                old_max_profit = row["max_profit"]
                if old_max_profit and old_max_profit != 0:
                    ratio = actual_quantity / (old_max_profit / entry_premium) if entry_premium else 1
                    new_max_loss = old_max_loss * ratio if old_max_loss else None
                else:
                    new_max_loss = old_max_loss

                # Update trade record
                cursor.execute(
                    """
                    UPDATE trades SET
                        quantity = ?,
                        max_profit = ?,
                        max_loss = ?,
                        updated_at = datetime('now')
                    WHERE trade_id = ?
                    """,
                    (actual_quantity, new_max_profit, new_max_loss, trade_id),
                )

                # Update the ENTRY order row for this trade
                cursor.execute(
                    """
                    UPDATE orders SET
                        quantity = ?,
                        filled_quantity = ?,
                        updated_at = datetime('now')
                    WHERE trade_id = ? AND trade_direction = 'ENTRY'
                    """,
                    (actual_quantity, actual_quantity, trade_id),
                )

                conn.commit()

            logger.info(
                f"Trade quantity updated: {trade_id} | "
                f"new qty: {actual_quantity} | "
                f"new max_profit: {new_max_profit} | new_max_loss: {new_max_loss}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to update trade quantity {trade_id}: {e}")
            return False
    
    # =========================================================================
    # MIGRATION HELPERS
    # =========================================================================
    
    def migrate_from_drawdown_recorder(self, old_db_path: str = "data/trade_drawdowns.db"):
        """
        Migrate data from old DrawdownRecorder database.
        
        Maps:
        - trade_date, entry_time → entry_time
        - exit_time → exit_time
        - max_drawdown → max_unrealized_loss
        - final_result → pnl
        - short_strike, long_strike → strikes
        - entry_premium → entry_premium_per_contract
        """
        if not os.path.exists(old_db_path):
            logger.info(f"No old drawdown DB found at {old_db_path}")
            return
        
        try:
            old_conn = sqlite3.connect(old_db_path)
            old_conn.row_factory = sqlite3.Row
            cursor = old_conn.cursor()
            
            cursor.execute("SELECT * FROM trade_drawdowns ORDER BY trade_date, entry_time")
            rows = cursor.fetchall()
            
            migrated = 0
            for row in rows:
                trade_id = f"MIGRATED-{row['trade_date']}-{row['entry_time'].replace(':', '')}"
                
                # Check if already migrated
                existing = self.get_trade(trade_id)
                if existing:
                    continue
                
                entry_time = f"{row['trade_date']}T{row['entry_time']}Z"
                exit_time = f"{row['trade_date']}T{row['exit_time']}Z" if row['exit_time'] else None
                
                strikes = []
                if row.get('short_strike'):
                    strikes.append(f"{row['short_strike']}S")
                if row.get('long_strike'):
                    strikes.append(f"{row['long_strike']}L")
                
                self.start_trade(
                    trade_id=trade_id,
                    strategy_id=row.get('strategy_id', 'SPX_15Min_Range'),
                    instrument_id="MIGRATED",
                    trade_type="CREDIT_SPREAD",
                    entry_price=row.get('entry_premium', 0) / 100,
                    quantity=1,
                    direction="LONG",
                    entry_time=entry_time,
                    strikes=strikes if strikes else None,
                    entry_premium_per_contract=row.get('entry_premium'),
                )
                
                if row.get('max_drawdown'):
                    self.update_trade_metrics(trade_id, row['max_drawdown'])
                
                if exit_time and row.get('final_result') is not None:
                    pnl = row['final_result']
                    exit_price = (row.get('entry_premium', 0) - pnl) / 100
                    self.close_trade(
                        trade_id=trade_id,
                        exit_price=exit_price,
                        exit_reason="MIGRATED",
                        exit_time=exit_time,
                    )
                
                migrated += 1
            
            old_conn.close()
            logger.info(f"Migrated {migrated} trades from DrawdownRecorder")
            
        except Exception as e:
            logger.error(f"Migration failed: {e}")
