# TODO

## System
- [ ] Fix UI status of IB connection. As of now it in not taken into account 
- [ ] [Logging] Log more nautilus trader events in app.log. For example, issues with IBKR connection are not logged to the app.log
- [ ] Add Risk management to the system

## New strategies
- [ ] [Options] SPX ORB 15 min strategy for SPX using vertical credit spreads
- [ ] [Futures] MNQ ORB 5 min + Fair Value Gap (FVG)


## Strategies enhancements
- [x] Get first SPX option trade working
- [ ] [Options] - check bid/ask spread before posting an order, add a condition for the spread side. It is relevant for option spread instruments

## UI
- [ ] [Analytics] Add strategy analytics section: strategy selector, equity curve, statistics. Read data from trades.db
- [ ] [Strategies] Improve logs for strategies on UI. As of now UI reads ony the first 500 lines from app.log. Hence, all logs from the strategy are not displayed

## Backtesting
- [ ] Create backtesting engine
- [ ] Get first backtesting results
- [ ] Create Buy & Hold strategy for comparison
- [ ] Load NQ historical data
- [ ] Load SPY historical data
- [ ] Load QQQ historical data
