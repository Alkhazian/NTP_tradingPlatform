"""
Data Actors Package

This package contains **Data Actors** - specialized components that provide market data feeds to trading strategies.

## What are Data Actors?

Data Actors are **non-trading components** that:
- Subscribe to market data from brokers/exchanges
- Process and transform raw data
- Broadcast processed data via Redis for consumption by trading strategies
- **Do NOT execute trades** or manage positions

## Why separate from Strategies?

- **Separation of Concerns**: Data acquisition is separate from trading logic
- **Reusability**: One actor can feed multiple strategies
- **Testability**: Strategies can be tested with mock data
- **Scalability**: Actors can run independently

## Technical Implementation

Data Actors inherit from BaseStrategy to leverage NautilusTrader infrastructure (data subscriptions, lifecycle management), but are architecturally separated:
- Located in backend/app/actors/ (not strategies/)
- Hidden from UI strategy list
- No trading logic (orders, positions, PnL)

See README.md for detailed documentation and examples.

## Current Actors

- **SPX Streamer** (spx_streamer.py) - Real-time S&P 500 Index price feed
"""
