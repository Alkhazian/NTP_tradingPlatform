# NTD - Trader Dashboard

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/Alkhazian/NTP_tradingPlatform)

A containerized trading dashboard built with NautilusTrader integration in mind.

## Components

- **Backend**: FastAPI (Python 3.12)
- **Frontend**: React + Vite + TailwindCSS + Shadcn/UI
- **Database**: Redis (Pub/Sub)
- **Gateway**: IB Gateway (Dockerized)

## Prerequisites

- Docker & Docker Compose

## Getting Started

1. **Configure IB Gateway**:
   Edit `docker-compose.yml` to set your TWS/Gateway credentials if needed.
   Default is Paper Trading mode with user/password placeholders.

2. **Run the Stack**:
   ```bash
   docker-compose up --build
   ```

3. **Access the Dashboard**:
   Open [http://localhost:5173](http://localhost:5173)

   The system will attempt to connect to IB Gateway.
   - **Net Liquidation**: Displays the account value.
   - **System Status**: Shows connection health of Backend, Redis, and IB Gateway.

## Development

- **Frontend**: Located in `frontend/`. Run `npm install` and `npm run dev` locally.
- **Backend**: Located in `backend/`. Run `uvicorn app.main:app --reload` locally.

## Project Structure

- `frontend/`: React application
- `backend/`: FastAPI application
- `docker-compose.yml`: Orchestration

## Notes

- The system uses `ibapi` to fetch the Net Liquidation value immediately upon connection.
- Real-time updates are streamed via WebSockets from Backend to Frontend, brokered by Redis.
