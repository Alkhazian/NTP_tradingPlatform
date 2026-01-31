#!/usr/bin/env python3
"""
Migration script to transfer data from old databases to the new unified TradingDataService.

Migrates from:
- data/trades.db (TradeRecorder)
- data/trade_drawdowns.db (DrawdownRecorder)

To:
- data/trading.db (TradingDataService)
"""

import sqlite3
import os
import json
import sys
from datetime import datetime

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TRADES_DB_PATH = "data/trades.db"
DRAWDOWNS_DB_PATH = "data/trade_drawdowns.db"
NEW_DB_PATH = "data/trading.db"


def migrate_trades():
    """Migrate data from old trades.db"""
    
    if not os.path.exists(TRADES_DB_PATH):
        print(f"⚠️ trades.db not found at {TRADES_DB_PATH}")
        return 0
    
    print(f"\n📦 Migrating from {TRADES_DB_PATH}...")
    
    # Ensure new DB exists
    from app.services.trading_data_service import TradingDataService
    trading_data = TradingDataService(db_path=NEW_DB_PATH)
    
    old_conn = sqlite3.connect(TRADES_DB_PATH)
    old_conn.row_factory = sqlite3.Row
    cursor = old_conn.cursor()
    
    cursor.execute("SELECT * FROM trades ORDER BY id")
    records = cursor.fetchall()
    print(f"   Found {len(records)} records")
    
    migrated = 0
    for rec in records:
        try:
            # Generate unique trade_id
            entry_time = rec['entry_time'] or datetime.now().isoformat()
            trade_id = f"T-OLD-{rec['id']}-{rec['strategy_id'][:8]}"
            
            # Check if exists
            with trading_data._get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT 1 FROM trades WHERE trade_id = ?", (trade_id,))
                if c.fetchone():
                    continue
            
            # Determine result
            pnl = rec['pnl'] or 0
            result = rec['result']
            if not result:
                result = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "EVEN")
            
            status = "CLOSED" if rec['exit_time'] else "OPEN"
            
            # Calculate duration
            duration = 0
            if rec['exit_time'] and rec['entry_time']:
                try:
                    entry_dt = datetime.fromisoformat(rec['entry_time'])
                    exit_dt = datetime.fromisoformat(rec['exit_time'])
                    duration = int((exit_dt - entry_dt).total_seconds())
                except:
                    pass
            
            with trading_data._get_connection() as conn:
                c = conn.cursor()
                c.execute("""
                    INSERT INTO trades (
                        trade_id, strategy_id, instrument_id, trade_type,
                        entry_time, exit_time, duration_seconds,
                        entry_price, exit_price, quantity, direction,
                        pnl, net_pnl, commission, result, status,
                        exit_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade_id,
                    rec['strategy_id'],
                    rec['instrument_id'],
                    rec['trade_type'] or "DAYTRADE",
                    rec['entry_time'],
                    rec['exit_time'],
                    duration,
                    rec['entry_price'],
                    rec['exit_price'],
                    rec['quantity'],
                    rec['direction'],
                    pnl,
                    pnl - (rec['commission'] or 0),
                    rec['commission'] or 0,
                    result,
                    status,
                    rec['exit_reason']
                ))
                conn.commit()
            migrated += 1
            
        except Exception as e:
            print(f"   ❌ Error migrating trade {rec['id']}: {e}")
    
    old_conn.close()
    return migrated


def migrate_drawdowns():
    """Migrate data from old trade_drawdowns.db"""
    
    if not os.path.exists(DRAWDOWNS_DB_PATH):
        print(f"⚠️ trade_drawdowns.db not found at {DRAWDOWNS_DB_PATH}")
        return 0
    
    print(f"\n📦 Migrating from {DRAWDOWNS_DB_PATH}...")
    
    from app.services.trading_data_service import TradingDataService
    trading_data = TradingDataService(db_path=NEW_DB_PATH)
    
    old_conn = sqlite3.connect(DRAWDOWNS_DB_PATH)
    old_conn.row_factory = sqlite3.Row
    cursor = old_conn.cursor()
    
    cursor.execute("SELECT * FROM trade_drawdowns ORDER BY id")
    records = cursor.fetchall()
    print(f"   Found {len(records)} records")
    
    migrated = 0
    for rec in records:
        try:
            # Generate unique trade_id
            trade_date = rec['trade_date'] or datetime.now().strftime("%Y-%m-%d")
            entry_time = rec['entry_time'] or "09:45:00"
            date_part = trade_date.replace("-", "")
            time_part = entry_time.replace(":", "")[:6]
            trade_id = f"T-DD-{rec['id']}-{date_part}-{time_part}"
            
            # Check if exists
            with trading_data._get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT 1 FROM trades WHERE trade_id = ?", (trade_id,))
                if c.fetchone():
                    continue
            
            strategy_id = rec['strategy_id'] or "SPX_15Min_Range"
            
            # Build strikes
            strikes = None
            if rec['short_strike'] and rec['long_strike']:
                strikes = json.dumps([str(int(rec['short_strike'])), str(int(rec['long_strike']))])
            
            # Entry price from premium
            entry_premium = rec['entry_premium'] or 0
            entry_price = entry_premium / 100.0
            
            # Build timestamps
            entry_time_iso = f"{trade_date}T{entry_time}"
            exit_time_str = rec['exit_time'] or "15:00:00"
            exit_time_iso = f"{trade_date}T{exit_time_str}" if rec['exit_time'] else None
            
            # Duration
            duration = 0
            if exit_time_iso:
                try:
                    entry_dt = datetime.fromisoformat(entry_time_iso)
                    exit_dt = datetime.fromisoformat(exit_time_iso)
                    duration = int((exit_dt - entry_dt).total_seconds())
                except:
                    pass
            
            # P&L and result
            pnl = rec['final_result'] or 0
            result = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "EVEN")
            status = "CLOSED" if rec['exit_time'] else "CANCELLED"
            
            with trading_data._get_connection() as conn:
                c = conn.cursor()
                c.execute("""
                    INSERT INTO trades (
                        trade_id, strategy_id, instrument_id, trade_type,
                        entry_time, exit_time, duration_seconds,
                        entry_price, quantity, direction,
                        pnl, net_pnl, result, status,
                        max_unrealized_loss, strikes, entry_premium_per_contract
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade_id,
                    strategy_id,
                    "SPX-SPREAD.CBOE",
                    "CREDIT_SPREAD",
                    entry_time_iso,
                    exit_time_iso,
                    duration,
                    entry_price,
                    1,  # default qty
                    "LONG",
                    pnl,
                    pnl,
                    result,
                    status,
                    rec['max_drawdown'],
                    strikes,
                    entry_premium
                ))
                conn.commit()
            migrated += 1
            
        except Exception as e:
            print(f"   ❌ Error migrating drawdown {rec['id']}: {e}")
    
    old_conn.close()
    return migrated


def cleanup_old_dbs():
    """Remove old database files"""
    removed = []
    for db_path in [TRADES_DB_PATH, DRAWDOWNS_DB_PATH]:
        if os.path.exists(db_path):
            os.remove(db_path)
            removed.append(db_path)
            print(f"   🗑️ Removed: {db_path}")
    return removed


def main():
    os.chdir(os.path.join(os.path.dirname(__file__), ".."))
    
    print("=" * 50)
    print("   Trading Data Migration Script")
    print("=" * 50)
    
    trades_migrated = migrate_trades()
    print(f"   ✅ Migrated {trades_migrated} trades from trades.db")
    
    drawdowns_migrated = migrate_drawdowns()
    print(f"   ✅ Migrated {drawdowns_migrated} drawdowns from trade_drawdowns.db")
    
    print("\n🧹 Cleaning up old databases...")
    cleanup_old_dbs()
    
    # Show final stats
    print("\n" + "=" * 50)
    print("   Migration Complete!")
    print("=" * 50)
    
    if os.path.exists(NEW_DB_PATH):
        conn = sqlite3.connect(NEW_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM trades")
        total = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM orders")
        orders = cursor.fetchone()[0]
        conn.close()
        print(f"   📊 New database: {NEW_DB_PATH}")
        print(f"   📈 Total trades: {total}")
        print(f"   📋 Total orders: {orders}")


if __name__ == "__main__":
    main()
