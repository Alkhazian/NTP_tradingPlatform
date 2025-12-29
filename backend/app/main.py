from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import logging
import os
import json
from .engine.redis_client import RedisClient
from .engine.system import SystemEngine
from .strategies.manager import StrategyManager
from .logging.service import setup_logging

# Setup centralized logging
setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Modules
redis_client = RedisClient()

# Connect to IB Gateway using environment variables
IB_HOST = os.getenv("IB_GATEWAY_HOST", "ib-gateway")
IB_PORT = int(os.getenv("IB_GATEWAY_PORT", "4002"))
system_engine = SystemEngine(host=IB_HOST, port=IB_PORT)
strategy_manager = StrategyManager(engine=system_engine, redis_client=redis_client)

# Register Dummy Strategy for demo
from .strategies.implementations.dummy_strategy import DummyStrategy, DummyStrategyConfig
strategy_manager.register_strategy("DummyStrategy", DummyStrategy, {"param1": "test", "stop_loss": 50.0})

# Register MES ORB Strategy
from .strategies.implementations.mes_orb_strategy import MesOrbStrategy, MesOrbStrategyConfig
strategy_manager.register_strategy("MesOrbStrategy", MesOrbStrategy, {
    "instrument_id": "MES.FUT-202403-GLOBEX",
    "bar_type": "MES.FUT-202403-GLOBEX-1-MINUTE-MID-EXTERNAL",
    "stop_loss_points": 10.0,
    "trailing_loss_points": 15.0,
    "orb_period_minutes": 15,
    "contract_quantity": 1
})

@app.on_event("startup")
async def startup_event():
    await redis_client.connect()
    logger.info("Starting System Engine...")
    try:
        await system_engine.start(pre_build_hook=strategy_manager.add_all_to_node)
        logger.info("System Engine started successfully")
    except Exception as e:
        logger.error(f"Failed to start System Engine: {e}")
    
    # Restore strategies
    await strategy_manager.restore_strategies()

    asyncio.create_task(broadcast_status())

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down System Engine...")
    await system_engine.stop()
    await redis_client.close()

async def broadcast_status():
    while True:
        try:
            # Update account state from SystemEngine
            await system_engine.update_status()
            
            status = system_engine.get_status()
            
            redis_status = False
            try:
                if redis_client.redis:
                    redis_status = await redis_client.redis.ping()
            except:
                pass
            
            status["redis_connected"] = redis_status
            status["backend_connected"] = True
            status["strategies"] = strategy_manager.get_strategies_status()
            
            # Publish to Redis channel
            if redis_client.redis:
                await redis_client.publish("system_status", status)
        except Exception as e:
            logger.error(f"Error in broadcast loop: {e}")
        
        await asyncio.sleep(1)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connection accepted")
    
    # Send initial status immediately
    status = system_engine.get_status()
    status["strategies"] = strategy_manager.get_strategies_status()
    try:
        if redis_client.redis:
            status["redis_connected"] = await redis_client.redis.ping()
    except:
        status["redis_connected"] = False
    
    await websocket.send_text(json.dumps(status))
    
    pubsub = None
    if redis_client.redis:
        pubsub = await redis_client.subscribe("system_status")

    try:
        if pubsub:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    await websocket.send_text(message["data"])
        else:
            # Fallback if Redis fails, just loop status
            while True:
                status = system_engine.get_status()
                status["redis_connected"] = False
                status["strategies"] = strategy_manager.get_strategies_status()
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
async def get_strategies():
    return strategy_manager.get_strategies_status()

@app.post("/strategies/{name}/start")
async def start_strategy(name: str):
    success = await strategy_manager.start_strategy(name)
    if not success:
        raise HTTPException(status_code=400, detail=f"Failed to start strategy {name}. Check logs/status for errors.")
    return {"status": "started", "name": name}

@app.post("/strategies/{name}/pause")
async def pause_strategy(name: str):
    success = await strategy_manager.pause_strategy(name)
    if not success:
        raise HTTPException(status_code=404, detail="Strategy not found or cannot be paused")
    return {"status": "paused", "name": name}

@app.post("/strategies/{name}/resume")
async def resume_strategy(name: str):
    success = await strategy_manager.resume_strategy(name)
    if not success:
        raise HTTPException(status_code=404, detail="Strategy not found or cannot be resumed")
    return {"status": "resumed", "name": name}

@app.post("/strategies/{name}/stop")
async def stop_strategy(name: str, force: bool = False):
    success = await strategy_manager.stop_strategy(name, force=force)
    if not success:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return {"status": "stopped", "name": name, "force": force}

@app.post("/strategies/stop_all")
async def stop_all_strategies():
    await strategy_manager.stop_all_strategies()
    return {"status": "all_stopped"}

@app.post("/strategies/{name}/config")
async def update_strategy_config(name: str, config: dict = Body(...)):
    # In a real app, this would update configs dynamically
    # For now, just logging it
    logger.info(f"Updating config for {name}: {config}")
    return {"status": "updated", "config": config}


