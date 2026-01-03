from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import logging
import os
from .redis_manager import RedisManager
from .nautilus_manager import NautilusManager
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

redis_manager = RedisManager()
# Connect to IB Gateway using environment variables
IB_HOST = os.getenv("IB_GATEWAY_HOST", "ib-gateway")
IB_PORT = int(os.getenv("IB_GATEWAY_PORT", "4002"))
nautilus_manager = NautilusManager(host=IB_HOST, port=IB_PORT)

@app.on_event("startup")
async def startup_event():
    await redis_manager.connect()
    logger.info("Starting NautilusTrader Manager...")
    try:
        await nautilus_manager.start()
        logger.info("NautilusTrader started successfully")
    except Exception as e:
        logger.error(f"Failed to start NautilusTrader: {e}")
    
    asyncio.create_task(broadcast_status())

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down NautilusTrader...")
    await nautilus_manager.stop()
    await redis_manager.close()

async def broadcast_status():
    while True:
        try:
            # Update account state from NautilusTrader
            await nautilus_manager.update_status()
            
            status = nautilus_manager.get_status()
            
            redis_status = False
            try:
                if redis_manager.redis:
                    redis_status = await redis_manager.redis.ping()
            except:
                pass
            
            status["redis_connected"] = redis_status
            status["backend_connected"] = True
            
            # Publish to Redis channel
            if redis_manager.redis:
                await redis_manager.publish("system_status", status)
        except Exception as e:
            logger.error(f"Error in broadcast loop: {e}")
        
        await asyncio.sleep(1)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connection accepted")
    
    # Send initial status immediately
    status = nautilus_manager.get_status()
    try:
        if redis_manager.redis:
            status["redis_connected"] = await redis_manager.redis.ping()
    except:
        status["redis_connected"] = False
    
    await websocket.send_text(json.dumps(status))
    
    pubsub = None
    if redis_manager.redis:
        pubsub = await redis_manager.subscribe("system_status")

    try:
        if pubsub:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    await websocket.send_text(message["data"])
        else:
            # Fallback if Redis fails, just loop status
            while True:
                status = nautilus_manager.get_status()
                status["redis_connected"] = False
                await websocket.send_text(json.dumps(status))
                await asyncio.sleep(1)
                
    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        if pubsub:
            await pubsub.close()

@app.get("/health")
async def health():
    return {"status": "ok"}


# ============ Strategy API Endpoints ============

from pydantic import BaseModel

class StartStrategyRequest(BaseModel):
    strike_offset: int = 0
    days_to_expiry: int = 0
    refresh_interval_seconds: int = 60

@app.post("/strategy/start")
async def start_strategy(request: StartStrategyRequest = StartStrategyRequest()):
    """
    Start the SPX 0DTE Straddle Strategy.
    
    This endpoint starts the strategy which will:
    1. Subscribe to SPX.CBOE price data
    2. Log all received data for diagnostics
    3. Track current price for UI display
    """
    try:
        result = await nautilus_manager.start_spx_strategy(
            strike_offset=request.strike_offset,
            days_to_expiry=request.days_to_expiry,
            refresh_interval_seconds=request.refresh_interval_seconds
        )
        return result
    except Exception as e:
        logger.error(f"Error starting strategy: {e}")
        return {"success": False, "error": str(e)}


@app.post("/strategy/stop")
async def stop_strategy():
    """
    Stop the SPX 0DTE Straddle Strategy.
    
    This endpoint stops the strategy and unsubscribes from market data.
    """
    try:
        result = await nautilus_manager.stop_spx_strategy()
        return result
    except Exception as e:
        logger.error(f"Error stopping strategy: {e}")
        return {"success": False, "error": str(e)}


@app.get("/strategy/status")
async def get_strategy_status():
    """
    Get the current strategy status.
    
    Returns:
        Strategy status including:
        - is_active: Whether the strategy is running
        - current_price: Current SPX price
        - logs: Recent strategy log entries
    """
    return nautilus_manager.get_strategy_status()


@app.get("/strategy/logs")
async def get_strategy_logs():
    """
    Get strategy logs only (for lightweight polling).
    """
    status = nautilus_manager.get_strategy_status()
    return {"logs": status.get("logs", [])}


@app.post("/strategy/mock-tick")
async def send_mock_tick(price: float = 5950.50):
    """
    Send a mock price tick to the strategy for testing.
    
    This endpoint is useful for testing while the market is closed.
    It simulates receiving a price update from the broker.
    
    Args:
        price: The mock SPX price to send (default: 5950.50)
    """
    try:
        result = await nautilus_manager.inject_mock_price(price)
        return result
    except Exception as e:
        logger.error(f"Error sending mock tick: {e}")
        return {"success": False, "error": str(e)}

