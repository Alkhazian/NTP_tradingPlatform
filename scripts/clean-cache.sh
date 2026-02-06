#!/bin/bash
# Script to clear NautilusTrader runtime state and Redis cache

echo "🧹 Cleaning session cache..."

# Determine the project root (where the script is located, one level up)
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( dirname "$SCRIPT_DIR" )"

# 1. Clear Strategy Runtime State (Filesystem)
# This removes active_trade_id and other persistent logic flags
STATE_DIR="$PROJECT_ROOT/data/strategies/state"
if [ -d "$STATE_DIR" ]; then
    echo "  -> Clearing strategy state files in $STATE_DIR..."
    rm -f $STATE_DIR/*.json
else
    echo "  -> Warning: State directory $STATE_DIR not found."
fi

# 2. Clear Nautilus Cache (Redis)
# This removes positions, orders, and market data snapshots
echo "  -> Flushing Redis cache..."
if command -v docker &> /dev/null; then
    # Try to execute via docker compose if available in the current context
    cd "$PROJECT_ROOT"
    if docker compose ps redis --format json | grep -q '"State":"running"'; then
        docker compose exec -T redis redis-cli FLUSHALL
    else
        echo "  -> Warning: Redis container not running via docker-compose. Skipping Redis flush."
    fi
else
    echo "  -> Warning: docker command not found. Skipping Redis flush."
fi

echo "✨ Session cache cleaned. Please restart the backend services."