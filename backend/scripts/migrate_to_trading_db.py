#!/usr/bin/env python3
"""
Migration script to transfer data from old drawdown_recorder database 
to the new unified TradingDataService database.

Run this script once before deploying the updated application.
"""

import sqlite3
import os
import json
from datetime import datetime

OLD_DB_PATH = "data/trade_drawdowns.db"
NEW_DB_PATH = "data/trading.db"

def migrate():
    """Migrate data from trade_drawdowns.db to trading.db"""
    
    if not os.path.exists(OLD_DB_PATH):
        print(f"⚠️ Old database not found at {OLD_DB_PATH}")
        print("   Nothing to migrate.")
        return False
    
    print(f"📦 Starting migration from {OLD_DB_PATH} to {NEW_DB_PATH}")
    
    # Ensure new DB exists with proper schema
    from app.services.trading_data_service import TradingDataService
    trading_data = TradingDataService(db_path=NEW_DB_PATH)
    
    # Connect to old database
    old_conn = sqlite3.connect(OLD_DB_PATH)
    old_conn.row_factory = sqlite3.Row
    old_cursor = old_conn.cursor()
    
    # Read old drawdown records
    old_cursor.execute("""
        SELECT 
            id, trade_date, entry_time, exit_time, short_strike, 
            long_strike, entry_premium, max_drawdown, final_result, strategy_id
        FROM trade_drawdowns
    """)
    
    old_records = old_cursor.fetchall()
    print(f"   Found {len(old_records)} records in old database")
    
    migrated_count = 0
    skipped_count = 0
    
    for record in old_records:
        try:
            # Generate trade_id from old record
            trade_date = record['trade_date'] or datetime.now().strftime("%Y-%m-%d")
            entry_time = record['entry_time'] or "00:00:00"
            
            # Create trade_id that matches old format
            date_part = trade_date.replace("-", "")
            time_part = entry_time.replace(":", "")[:6]
            trade_id = f"T-MIGRATED-{date_part}-{time_part}"
            
            # Check if already migrated
            with trading_data._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM trades WHERE trade_id = ?", (trade_id,))
                if cursor.fetchone():
                    skipped_count += 1
                    continue
            
            # Build strikes list
            short_strike = record['short_strike']
            long_strike = record['long_strike']
            strikes_json = None
            if short_strike and long_strike:
                strikes_json = json.dumps([str(int(short_strike)), str(int(long_strike))])
            
            # Determine entry price (premium)
            entry_premium = record['entry_premium'] or 0
            entry_price = entry_premium / 100.0  # Convert cents to dollars
            
            # Build entry and exit times
            entry_time_iso = f"{trade_date}T{entry_time}"
            exit_time_str = record['exit_time'] or "15:00:00"
            exit_time_iso = f"{trade_date}T{exit_time_str}"
            
            # Calculate duration
            try:
                entry_dt = datetime.fromisoformat(entry_time_iso)
                exit_dt = datetime.fromisoformat(exit_time_iso)
                duration_seconds = int((exit_dt - entry_dt).total_seconds())
            except:
                duration_seconds = 0
            
            # Get final P&L and determine result
            final_pnl = record['final_result'] or 0
            result = "WIN" if final_pnl > 0 else ("LOSS" if final_pnl < 0 else "EVEN")
            
            strategy_id = record['strategy_id'] or "unknown"
            
            # Insert into new database
            with trading_data._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO trades (
                        trade_id, strategy_id, instrument_id, trade_type,
                        entry_time, exit_time, duration_seconds,
                        entry_price, exit_price, quantity, direction,
                        pnl, net_pnl, result, status,
                        max_unrealized_loss, strikes,
                        entry_premium_per_contract
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade_id,
                    strategy_id,
                    "SPX-SPREAD.CBOE",  # Default instrument
                    "CREDIT_SPREAD",    # Default trade type
                    entry_time_iso,
                    exit_time_iso,
                    duration_seconds,
                    entry_price,
                    0.0,  # Exit price unknown
                    1,    # Default quantity
                    "LONG",  # Default direction
                    final_pnl,
                    final_pnl,  # net_pnl same as pnl (no commission data)
                    result,
                    "CLOSED",
                    record['max_drawdown'],
                    strikes_json,
                    entry_premium
                ))
                conn.commit()
            
            migrated_count += 1
            
        except Exception as e:
            print(f"   ❌ Error migrating record {record['id']}: {e}")
    
    old_conn.close()
    
    print(f"\n✅ Migration complete!")
    print(f"   Migrated: {migrated_count}")
    print(f"   Skipped (already exists): {skipped_count}")
    
    # Archive old database
    if migrated_count > 0:
        archive_path = OLD_DB_PATH.replace(".db", ".migrated.db")
        os.rename(OLD_DB_PATH, archive_path)
        print(f"   Old database archived to: {archive_path}")
    
    return True


if __name__ == "__main__":
    os.chdir("/root/ntd_trader_dashboard/backend")
    migrate()
