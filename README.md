# NTP - Trading Platform

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/Alkhazian/NTP_tradingPlatform)

A robust, containerized, and automated trading platform built around [NautilusTrader](https://github.com/nautechsystems/nautilus_trader). The system is designed for solo operation with a focus on reliability, simplicity, and debuggability.

## 🏗 Architecture & Components

The platform consists of several decoupled services orchestrated via Docker Compose:

- **Backend (FastAPI - Python 3.12)**
  - Manages NautilusTrader instances and streaming data.
  - Exposes RESTful endpoints for strategy management and trading analytics.
  - Streams real-time system status and logs to the frontend via WebSockets.
  - Manages SQLite persistence (`trading.db`) for isolated tracking of orders and trades.
  - Integrates asynchronous Telegram Bot notifications for live trade updates (Starts, Entries, Exits with PnL, etc.).

- **Frontend (React + Vite + TypeScript)**
  - Built with TailwindCSS, Shadcn/UI, and Lightweight Charts.
  - Provides a real-time dashboard for Net Liquidation, account status, and system health.
  - Includes a comprehensive Analytics view for strategy equity curves and statistics.
  - Secured with cookie-based session authentication.

- **Data & Messaging**
  - **Redis**: Acts as an event broker (Pub/Sub) between the NautilusTrader engine and the FastAPI WebSocket server.
  - **VictoriaLogs & Vector**: Lightweight, high-performance structured logging pipeline with 14-day retention.

- **Trading Gateway**
  - **IB Gateway**: Dockerized Interactive Brokers Gateway (`ghcr.io/gnzsnz/ib-gateway`) configured for paper or live trading, supporting 2FA via VNC.

## ✨ Key Features

- **Algorithmic Strategies**: Designed to run automated options and futures strategies (e.g., SPX ORB 15 min long call, SPX 1DTE Bull Put Spread).
- **Real-Time Observability**: Live-streaming logs and system statuses to the frontend UI.
- **Trade Analytics**: Dedicated SQLite database storing executed trades. Frontend pulls aggregated metrics (Win rate, max drawdown, net PnL, gross PnL, total commissions) and visualizes equity curves.
- **Robust Notifications**: Automated Telegram alerts keeping the operator informed of critical strategy state changes and trade entries/exits.
- **Resilience**: Fire-and-forget logging handlers and robust exception management to ensure the core trading engine operates unimpeded by peripheral service failures.

## 🚀 Getting Started

### Prerequisites
- Docker & Docker Compose
- Interactive Brokers Account (Paper Trading recommended for setup)
- Telegram Bot Token & Chat ID (for notifications)

### 1. Configuration
Create/Edit the `.env` file in the root directory to set up your credentials:

```ini
# IB Gateway
TWS_USERID=your_ib_username
TWS_PASSWORD=your_ib_password
TRADING_MODE=paper # or live

# Dashboard Security
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=securepassword

# Telegram Notifications
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```
*(Check `docker-compose.yml` for additional optional environment variables like 2FA VNC settings.)*

### 2. Run the Stack
```bash
docker-compose up --build -d
```
This will spin up Nginx, the UI, Backend API, Redis, IB Gateway, VictoriaLogs, and Vector.

### 3. Access the Dashboard
Navigate to [http://localhost](http://localhost) (or port 80). 
- Login using your configured `DASHBOARD_USER` and `DASHBOARD_PASSWORD`.
- The system will immediately connect to IB Gateway and display your Net Liquidation value.

## 💻 Development

- **Frontend (`/frontend`)**:
  ```bash
  cd frontend
  npm install
  npm run dev
  ```
- **Backend (`/backend`)**:
  ```bash
  cd backend
  python -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt
  uvicorn app.main:app --reload
  ```

## 🗺 Roadmap / TODOs

Active development is tracked in `TODO.md`. Ongoing priorities include:
- Extended Risk Management mechanisms (daily loss limits, max position sizes).
- Unified Backtesting engine and scripts to benchmark against Buy & Hold.
- Handling of exchange holidays and custom trading calendars.
- Enhanced multi-instrument scaling per strategy.
