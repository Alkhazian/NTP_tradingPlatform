# Strategy Management & Lifecycle Logic

This document details the technical implementation of strategy lifecycle management within the NTP trading platform, covering both backend state transitions and frontend UI representations.

---

## 1. Strategy States (StrategyMode)

The system uses a set of explicit states to manage the lifecycle of automated trading strategies.

| State | Description | UI Badge |
| :--- | :--- | :--- |
| `STOPPED` | Initial state. No trading logic active. All daily variables are reset. | Grey (Secondary) |
| `STARTING` | Transitionary. Pre-flight checks (connection, risk) are in progress. | Grey + Pulse |
| `RUNNING` | Active trading. Strategy is evaluating market data and sending orders. | Emerald + Pulse |
| `PAUSED` | Suspended mode. Positions are held, but no new trades are initiated. | Amber (Warning) |
| `REDUCE_ONLY` | Safe exit state. Strategy only manages/closes existing positions; no new entries allowed. | Amber (Warning) |
| `STOPPING` | Transitionary. Shutdown sequence initiated (canceling orders, closing positions). | Grey + Pulse |
| `ERROR` | Failed state. Circuit breaker triggered after $N$ consecutive errors. | Red (Destructive) |

---

## 2. Transition Logic & Commands

### Start Strategy
- **Sequence**: `STOPPED` → `STARTING` → `RUNNING`.
- **Pre-flight Checks**:
    - Validates IBKR connection.
    - Verifies margin availability (Buying Power).
    - Checks for instrument conflicts.
- **Persistence**: Loads last known state from Redis (e.g., daily trade counts).

### Normal Stop (Graceful)
- **Logic**: If the account is already flat, transitions immediately to `STOPPED`. If positions exist, transitions to `REDUCE_ONLY`.
- **Auto-Transition**: Once the strategy reaches a flat position while in `REDUCE_ONLY` or `STOPPING`, it automatically flips to `STOPPED`.

### Force Stop (Emergency)
- **Logic**: Immediate transition to `STOPPING`.
- **Actions**: Cancels all outstanding orders and initiates immediate market closing orders for all open positions related to that strategy.
- **Result**: Once cleanup is complete, transitions directly to `STOPPED`.

### Pause / Resume
- **Logic**: Simple toggle between `RUNNING` and `PAUSED`.
- **Safety**: `on_bar` and other event handlers are guarded; they return early if the state is `PAUSED`.

---

## 3. Architecture & Safety

### Reliability Features
1. **Transitional State Protection**: The system never persists `STARTING` or `STOPPING` states. If a crash occurs during a transition, the strategy will recover as `STOPPED` to prevent deadlocks.
2. **Circuit Breaker**: Strategies track consecutive errors. If a threshold is met, the strategy self-terminates into an `ERROR` state and notifies the user.
3. **Guard Logic**: Entry logic in strategy implementations (e.g., `MesOrbStrategy`) is specifically guarded against `REDUCE_ONLY` and `STOPPING` states to prevent race conditions during shutdown.

---

## 4. UI Implications

### Dashboard Aggregation
The main Dashboard "Strategy Status" indicator uses an **Aggregated Logic** to provide a system-wide health check:
- **Green (Success)**: At least one strategy is `RUNNING`.
- **Yellow (Warning)**: No strategies are running, but some are `PAUSED` or `REDUCE_ONLY`.
- **Red (Stopped/Error)**: All strategies are `STOPPED`, or at least one is in an `ERROR` state with no others running to compensate.

### Strategy Controls
- **Contextual Buttons**: The UI dynamically hides/shows buttons based on state. For example, "Resume" only appears when `PAUSED`, and "Start" only appears when `STOPPED` or `ERROR`.
- **Confirmation Prompts**: High-risk actions like **EMERGENCY STOP ALL** require a browser confirmation to prevent accidental clicks.
