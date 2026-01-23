# TODO

## New strategies
- [x] [Options] SPX ORB 15 min long call - search strike using delta value
- [x] [Options] SPX ORB 15 min long call - search strike using $ value 
- [x] [Options] SPX ORB 15 min long put - search strike using $ value
- [x] [Options] SPX ORB 15 min 5 size with vertical credit spreads (call credit and put credit)
- [ ] [Options] SPX ORB 60 min 10 size with vertical credit spreads §(call credit and put credit)
- [ ] [Futures] MNQ ORB 5 min + Fair Value Gap (FVG)


## Strategies enhancements
- [x] Get first SPX option trade working
- [x] [Options] - check bid/ask spread before posting an order, add a condition for the spread size.
- [ ] Refactor trades storage to separate transation part (orders DB) and view part (trades).
- [ ] Manage open position handling for strategies that trade the same instrument.
- [ ] [Options] - for option spread instruments implement a limit order exit, instead of market order. E.g. post the order with mid-price as limit price and then increase steadily the limit price until the order is filled
- [ ] Add Risk Management
    - [ ] Create RiskManager class
    - [ ] Integrate pre-order validation
    - [ ] Add max position size check
    - [ ] Add daily loss limit check
    - [ ] Add max orders per day limit
- [ ] Refactor BaseStrategy
    - [ ] Remove dual config pattern (use only self.strategy_config)
    - [ ] Add lifecycle state machine
    - [ ] Implement on_instrument(), on_order_book() base handlers
- [ ] Strategy Manager Improvements
    - [ ] Fix terminal state handling (use enum, not string matching)
    - [ ] Add strategy versioning
    - [ ] Add rollback capability
    - [ ] Implement circuit breaker pattern
- [ ] Multi-Instrument Support
    - [ ] Refactor to support strategy trading multiple instruments
    - [ ] Add instrument-specific position tracking
    - [ ] Add cross-instrument risk checks

## UI
- [ ] [Analytics] Add strategy analytics section: strategy selector, equity curve, statistics. Read data from trades.db
- [x] [Strategies] Improve logs for strategies on UI. As of now UI reads ony the first 500 lines from app.log. Hence, all logs from the strategy are not displayed
- [ ] [Strategies] Add unrealized P&L to strategy stats/status

## Backtesting
- [ ] Create backtesting engine
- [ ] Get first backtesting results
- [ ] Create Buy & Hold strategy for comparison
- [ ] Load NQ historical data
- [ ] Load SPY historical data
- [ ] Load QQQ historical data

## System
- [ ] Fix UI status of IB connection. As of now it in not taken into account 
- [ ] [Logging] Log more nautilus trader events in app.log. For example, issues with IBKR connection are not logged to the app.log
- [x] [Logging] Re-do logging on UI to use API and lazy-loading to search fot the full history. Implement DB storage of logs.
- [ ] Integrate with exchange_calendars library or maintain a holiday list. Update strategy logic to account for holidays
- [ ] [Logging] ? Remove custom logger (use inherited Nautilus logger)
- [ ] Enhanced Configuration
    - [ ] Create strategy-specific config classes
    - [ ] Add Pydantic validators
    - [ ] Add config schema documentation
    - [ ] Support config hot reload (stop/update/start)

## Monitoring & Observability
- [ ] Add performance metrics collection
- [ ] Add health check endpoint
- [ ] Log all order flow with correlation IDs

## Testing
- [ ] Add unit tests for strategy logic
- [ ] Add integration tests with Nautilus backtest
- [ ] Add state persistence tests
- [ ] Add failure recovery tests

## Performance Optimization
- [ ] Profile hot paths
- [ ] Optimize bar processing
- [ ] Cache frequently accessed data
- [ ] Reduce allocation overhead

## Documentation
- [ ] Document strategy development guide
- [ ] Add configuration examples
- [ ] Document state machine transitions
- [ ] Add troubleshooting guide
- [ ] Add backtesting guide
