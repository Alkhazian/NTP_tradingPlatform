from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import logging
import os
from .redis_manager import RedisManager
from .nautilus_manager import NautilusManager
from .strategies.config import StrategyConfig
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

# Strategy Management Endpoints

@app.get("/strategies")
async def list_strategies():
    if not nautilus_manager.strategy_manager:
        return []
    return nautilus_manager.strategy_manager.get_all_strategies_status()

@app.post("/strategies")
async def create_strategy(config: dict):
    """
    Generic endpoint to create any supported strategy.
    The config dictionary must contain a 'parameters' dict with a 'strategy_type' field
    if it's not a standard strategy, or we infer it.
    For this implementation, we expect the client to align with the StrategyConfig structure.
    """
    if not nautilus_manager.strategy_manager:
        raise HTTPException(status_code=503, detail="System not ready")
    
    # We manually parse the dict into the appropriate Pydantic model
    # in the manager, or we let the manager handle the dictionary directly.
    # To keep the manager clean (which expects Pydantic objects), 
    # we can try to guess here or pass the dict to a new manager method.
    
    # Let's do a quick check for type here to cast it safely
    try:
        validated_config = StrategyConfig(**config)
            
        await nautilus_manager.strategy_manager.create_strategy(validated_config, auto_start=validated_config.enabled)
        return {"status": "created", "id": validated_config.id}
        
    except Exception as e:
        logger.error(f"Error creating strategy: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/strategies/{strategy_id}/start")
async def start_strategy(strategy_id: str):
    if not nautilus_manager.strategy_manager:
        raise HTTPException(status_code=503, detail="System not ready")
    
    await nautilus_manager.strategy_manager.start_strategy(strategy_id)
    return {"status": "started", "id": strategy_id}

@app.post("/strategies/{strategy_id}/stop")
async def stop_strategy(strategy_id: str):
    if not nautilus_manager.strategy_manager:
        raise HTTPException(status_code=503, detail="System not ready")
    
    await nautilus_manager.strategy_manager.stop_strategy(strategy_id)
    return {"status": "stopped", "id": strategy_id}

@app.put("/strategies/{strategy_id}")
async def update_strategy(strategy_id: str, config: dict):
    if not nautilus_manager.strategy_manager:
        raise HTTPException(status_code=503, detail="System not ready")
        
    try:
        updated_strategy = await nautilus_manager.strategy_manager.update_strategy_config(strategy_id, config)
        if not updated_strategy:
             raise HTTPException(status_code=404, detail="Strategy not found")
        return {"status": "updated", "id": strategy_id}
    except Exception as e:
        logger.error(f"Error updating strategy: {e}")
        raise HTTPException(status_code=400, detail=str(e))


