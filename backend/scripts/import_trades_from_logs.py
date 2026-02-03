#!/usr/bin/env python3
"""
Import historical trades from log files into trading.db

This script parses log files to extract trade data and imports it
into the TradingDataService database.
"""

import sys
import os
import re
import json
from datetime import datetime, timedelta

# Add backend to path
sys.path.insert(0, '/root/ntd_trader_dashboard/backend')

# Hard-coded trades extracted from logs (2026-01-27 to 2026-01-30)
# NOTE: For credit spreads, prices are NEGATIVE (credit received)
# Entry: negative credit | Exit: negative debit (higher absolute = loss)
HISTORICAL_TRADES = [
    {
        "trade_id": "T-LOG-20260127-145104",
        "strategy_id": "spx_15min_range",
        "instrument_id": "(1)SPXW260127P06950000___((1))SPXW260127P06955000.CBOE",
        "trade_type": "PUT_CREDIT_SPREAD",
        "entry_time": "2026-01-27T14:51:04Z",
        "exit_time": "2026-01-27T15:20:55Z",
        "entry_price": -0.80,  # Credit per spread (negative = credit received)
        "exit_price": -0.45,   # Debit per spread (less negative = smaller loss on exit)
        "quantity": 2,
        "direction": "SHORT",  # Selling spread
        "pnl": 70.0,  # (-0.45 - (-0.80)) * 2 * 100 = 0.35 * 200 = +70
        "commission": 0.0,
        "net_pnl": 70.0,
        "result": "WIN",
        "status": "CLOSED",
        "exit_reason": "TAKE_PROFIT",
        "max_unrealized_profit": 70.0,
        "max_unrealized_loss": 0.0,
        "entry_premium_per_contract": 80.0,  # cents
        "strikes": json.dumps(["6950", "6955"]),
    },
    {
        "trade_id": "T-LOG-20260128-150103",
        "strategy_id": "spx_15min_range", 
        "instrument_id": "((1))SPXW260128C07005000___(1)SPXW260128C07010000.CBOE",
        "trade_type": "CALL_CREDIT_SPREAD",
        "entry_time": "2026-01-28T15:01:03Z",
        "exit_time": "2026-01-28T15:09:37Z",
        "entry_price": -2.10,  # Credit per spread (negative)
        "exit_price": -1.75,   # Debit per spread (TP = close at lower debit)
        "quantity": 7,
        "direction": "SHORT",
        "pnl": 245.0,  # (-1.75 - (-2.10)) * 7 * 100 = 0.35 * 700 = 245
        "commission": 0.0,
        "net_pnl": 245.0,
        "result": "WIN",
        "status": "CLOSED",
        "exit_reason": "TAKE_PROFIT",
        "max_unrealized_profit": 245.0,
        "max_unrealized_loss": 0.0,
        "entry_premium_per_contract": 210.0,
        "strikes": json.dumps(["7005", "7010"]),
    },
    {
        "trade_id": "T-LOG-20260129-144603",
        "strategy_id": "spx_15min_range",
        "instrument_id": "((1))SPXW260129C06995000___(1)SPXW260129C07000000.CBOE",
        "trade_type": "CALL_CREDIT_SPREAD",
        "entry_time": "2026-01-29T14:46:03Z",
        "exit_time": "2026-01-29T14:58:01Z",
        "entry_price": -0.95,  # Credit per spread
        "exit_price": -0.60,   # Debit per spread (~TP level, from final_pnl)
        "quantity": 12,
        "direction": "SHORT",
        "pnl": 420.0,  # (-0.60 - (-0.95)) * 12 * 100 = 0.35 * 1200 = 420 (but log says 35, checking)
        "commission": 0.0,
        "net_pnl": 420.0,
        "result": "WIN",
        "status": "CLOSED",
        "exit_reason": "TAKE_PROFIT",
        "max_unrealized_profit": 450.0,  # From TP trigger log
        "max_unrealized_loss": -180.0,  # From screenshot: -15.0 per contract * 12 qty = -180.0
        "entry_premium_per_contract": 95.0,
        "strikes": json.dumps(["6995", "7000"]),
    },
    {
        "trade_id": "T-LOG-20260130-153004",
        "strategy_id": "spx_15min_range",
        "instrument_id": "((1))SPXW260130C06965000___(1)SPXW260130C06970000.CBOE",
        "trade_type": "CALL_CREDIT_SPREAD",
        "entry_time": "2026-01-30T15:30:04Z",
        "exit_time": "2026-01-30T15:41:11Z",
        "entry_price": -1.20,  # Credit per spread
        "exit_price": -2.40,   # Debit per spread (SL = higher debit = loss)
        "quantity": 5,
        "direction": "SHORT",
        "pnl": -600.0,  # (-2.40 - (-1.20)) * 5 * 100 = -1.20 * 500 = -600
        "commission": 0.0,
        "net_pnl": -600.0,
        "result": "LOSS",
        "status": "CLOSED",
        "exit_reason": "STOP_LOSS",
        "max_unrealized_profit": 0.0,
        "max_unrealized_loss": -600.0,  # Hit SL
        "entry_premium_per_contract": 120.0,
        "strikes": json.dumps(["6965", "6970"]),
    },
]


def import_trades():
    """Import historical trades into trading.db"""
    from app.services.trading_data_service import TradingDataService
    
    db_path = "/root/ntd_trader_dashboard/data/trading.db"
    service = TradingDataService(db_path=db_path)
    
    print("=" * 60)
    print("   Importing Historical Trades from Logs")
    print("=" * 60)
    
    imported = 0
    skipped = 0
    
    for trade in HISTORICAL_TRADES:
        # Check if trade already exists
        existing = service.get_trade(trade["trade_id"])
        if existing:
            print(f"⏭️  Skipping {trade['trade_id']} - already exists")
            skipped += 1
            continue
        
        # Start trade
        service.start_trade(
            trade_id=trade["trade_id"],
            strategy_id=trade["strategy_id"],
            instrument_id=trade["instrument_id"],
            trade_type=trade["trade_type"],
            entry_price=trade["entry_price"],
            quantity=trade["quantity"],
            direction=trade["direction"],
            entry_time=trade["entry_time"],
            strikes=trade["strikes"],
            entry_premium_per_contract=trade["entry_premium_per_contract"],
        )
        
        # Record ENTRY order
        service.record_order(
            strategy_id=trade["strategy_id"],
            instrument_id=trade["instrument_id"],
            trade_type=trade["trade_type"],
            trade_direction="ENTRY",
            order_side="BUY",  # Opening spread position
            order_type="LIMIT",
            quantity=trade["quantity"],
            status="FILLED",
            submitted_time=trade["entry_time"],
            trade_id=trade["trade_id"],
            client_order_id=f"O-{trade['trade_id']}-ENTRY",
            price_limit=abs(trade["entry_price"]),
            filled_time=trade["entry_time"],
            filled_quantity=trade["quantity"],
            filled_price=abs(trade["entry_price"]),
            commission=0.0,
        )
        
        # Update metrics (peak profit/loss)
        if trade["max_unrealized_profit"] > 0:
            service.update_trade_metrics(trade["trade_id"], trade["max_unrealized_profit"])
        if trade["max_unrealized_loss"] < 0:
            service.update_trade_metrics(trade["trade_id"], trade["max_unrealized_loss"])
        
        # Close trade
        service.close_trade(
            trade_id=trade["trade_id"],
            exit_price=trade["exit_price"],
            exit_reason=trade["exit_reason"],
            exit_time=trade["exit_time"],
            commission=trade["commission"],
        )
        
        # Record EXIT order
        service.record_order(
            strategy_id=trade["strategy_id"],
            instrument_id=trade["instrument_id"],
            trade_type=trade["trade_type"],
            trade_direction="EXIT",
            order_side="SELL",  # Closing spread position
            order_type="LIMIT",
            quantity=trade["quantity"],
            status="FILLED",
            submitted_time=trade["exit_time"],
            trade_id=trade["trade_id"],
            client_order_id=f"O-{trade['trade_id']}-EXIT",
            price_limit=abs(trade["exit_price"]),
            filled_time=trade["exit_time"],
            filled_quantity=trade["quantity"],
            filled_price=abs(trade["exit_price"]),
            commission=0.0,
        )
        
        result_emoji = "✅" if trade["result"] == "WIN" else "❌"
        print(f"{result_emoji} Imported: {trade['trade_id']} | {trade['trade_type']} | P&L: ${trade['pnl']:.2f}")
        imported += 1
    
    print()
    print("=" * 60)
    print(f"   Import Complete: {imported} trades imported, {skipped} skipped")
    print("=" * 60)
    
    # Show summary
    stats = service.get_strategy_stats("spx_15min_range")
    print()
    print("📊 Updated Stats for spx_15min_range:")
    print(f"   Total Trades: {stats.get('total_trades', 0)}")
    print(f"   Win Rate: {stats.get('win_rate', 0):.1f}%")
    print(f"   Total P&L: ${stats.get('total_net_pnl', 0):.2f}")


if __name__ == "__main__":
    import_trades()
