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

