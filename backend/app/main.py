from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import logging
import os
from .redis_manager import RedisManager
from .nautilus_manager import NautilusManager
from .strategies.config import StrategyConfig
import json

import aiofiles
from logging.handlers import RotatingFileHandler

# Import routers
from .routers import logs as logs_router

# Configure logging
LOG_FILE = "logs/app.log"
os.makedirs("logs", exist_ok=True)

# Rotating file handler: 10MB per file, 5 backups
file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=10 * 1024 * 1024, # 10MB
    backupCount=5
)
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))

# Configure the root logger to use both stdout and our file handler
logging.basicConfig(
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        file_handler
    ],
    # This force=True ensures basicConfig reconfigures if already set up
    force=True 
)

# For library loggers, we clear their existing handlers and ensure they propagate
# to the root logger where our file_handler is waiting.
for logger_name in ["uvicorn", "uvicorn.error", "nautilus_trader"]:
    l = logging.getLogger(logger_name)
    l.handlers = [] 
    l.propagate = True

# Silence noisy loggers (access logs and internal requests)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
logger.info("--- Log Stream Initialized ---")

# Configure VictoriaLogs handler (fire-and-forget)
# If VictoriaLogs isn't running, logs are silently dropped - trading unaffected
try:
    from .logging import VictoriaLogsHandler
    VICTORIALOGS_URL = os.getenv("VICTORIALOGS_URL", "http://victorialogs:9428")
    
    victorialogs_handler = VictoriaLogsHandler(
        victorialogs_url=VICTORIALOGS_URL,
        stream_fields=("strategy_id", "source", "level"),
        extra_fields={"app": "nautilus-trader"},
    )
    victorialogs_handler.setLevel(logging.DEBUG)
    
    # Add to root logger to capture all logs
    logging.getLogger().addHandler(victorialogs_handler)
    # Ensure strategy loggers use it
    #logging.getLogger("strategy").addHandler(victorialogs_handler)
    
    logger.info("VictoriaLogs handler configured")
except Exception as e:
    logger.warning(f"VictoriaLogs handler not configured: {e}")

app = FastAPI()

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Global exception caught: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"message": "Internal server error"},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(logs_router.router)

redis_manager = RedisManager()
# Connect to IB Gateway using environment variables
IB_HOST = os.getenv("IB_GATEWAY_HOST", "ib-gateway")
IB_PORT = int(os.getenv("IB_GATEWAY_PORT", "4002"))
nautilus_manager = NautilusManager(host=IB_HOST, port=IB_PORT)

# Event to trigger immediate status updates
update_trigger = asyncio.Event()

@app.on_event("startup")
async def startup_event():
    await redis_manager.connect()
    
    async def start_nautilus():
        logger.info("Starting NautilusTrader Manager...")
        try:
            await nautilus_manager.start()
            logger.info("NautilusTrader started successfully")
        except Exception as e:
            logger.error(f"Failed to start NautilusTrader: {e}")
            
    asyncio.create_task(start_nautilus())
    asyncio.create_task(broadcast_status())
    asyncio.create_task(nautilus_event_listener())

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down NautilusTrader...")
    await nautilus_manager.stop()
    await redis_manager.close()

async def nautilus_event_listener():
    """Listen for NautilusTrader events on Redis and trigger UI updates"""
    while True:
        try:
            if not redis_manager.redis:
                await asyncio.sleep(1)
                continue
                
            # Subscribe to all keys (or specific Nautilus patterns)
            # Nautilus usually publishes to capitalized class names e.g. "OrderFilled", "PositionChanged"
            # We listen to everything to be sure we catch state changes
            pubsub = await redis_manager.psubscribe("*")
            if not pubsub:
                await asyncio.sleep(1)
                continue
                
            logger.info("Started listening for Nautilus events on Redis...")
            
            async for message in pubsub.listen():
                if message["type"] == "pmessage":
                    channel = message["channel"]
                    # Ignore our own system_status channel to prevent loops
                    if channel == "system_status" or channel == "spx_stream_price":
                        continue
                        
                    # logger.info(f"Received Redis event on {channel}, triggering update...")
                    update_trigger.set()
                    
        except Exception as e:
            logger.error(f"Error in event listener: {e}")
            await asyncio.sleep(5)

async def broadcast_status():
    while True:
        try:
            # Update account state from NautilusTrader
            await nautilus_manager.update_status()
            
            status = await nautilus_manager.get_status()
            
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
                #logger.info("Broadcasted system status to Redis")
        except Exception as e:
            logger.error(f"Error in broadcast loop: {e}")
        
        # Wait for trigger OR 30 seconds (Heartbeat)
        try:
            # Debounce: If triggered, wait at least 500ms to aggregate multiple rapid events
            # But here we just wait for the next trigger
            await asyncio.wait_for(update_trigger.wait(), timeout=30.0)
            
            # If we woke up due to trigger, verify it's not a spam loop
            # and maybe debounce slightly if needed.
            # Simple debounce: wait 200ms and clear trigger
            await asyncio.sleep(0.2) 
            update_trigger.clear()
            
        except asyncio.TimeoutError:
            # Timeout reached, run loop again (Heartbeat)
            pass

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connection accepted")
    
    # Send initial status immediately
    status = await nautilus_manager.get_status()
    try:
        if redis_manager.redis:
            status["redis_connected"] = await redis_manager.redis.ping()
    except:
        status["redis_connected"] = False
    
    await websocket.send_text(json.dumps(status))
    
    pubsub = None
    if redis_manager.redis:
        pubsub = await redis_manager.subscribe("system_status", "spx_stream_price", "spx_stream_log")

    try:
        if pubsub:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    await websocket.send_text(message["data"])
        else:
            # Fallback if Redis fails, just loop status
            while True:
                status = await nautilus_manager.get_status()
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
@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    logger.info("Log stream connection accepted")
    
    try:
        if os.path.exists(LOG_FILE):
            async with aiofiles.open(LOG_FILE, mode='r') as f:
                # Read all current lines for history
                await f.seek(0, os.SEEK_SET)
                all_lines = await f.readlines()
                history = all_lines[-500:] if len(all_lines) > 500 else all_lines
                
                logger.info(f"Sending {len(history)} lines of log history")
                for line in history:
                    await websocket.send_text(line)

                # Pointer is at EOF, continue tailing
                while True:
                    line = await f.readline()
                    if line:
                        await websocket.send_text(line)
                    else:
                        await asyncio.sleep(0.1)
        else:
            logger.warning(f"Log file {LOG_FILE} not found for streaming")
            while True:
                await asyncio.sleep(1)
                    
    except WebSocketDisconnect:
        logger.info("Log stream disconnected")
    except Exception as e:
        logger.error(f"Error in log stream: {e}", exc_info=True)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/strategies/{strategy_id}/trades")
async def get_strategy_trades(strategy_id: str, limit: int = 100):
    if not nautilus_manager.strategy_manager:
        return []
    
    # Access TradeRecorder through NautilusManager
    recorder = getattr(nautilus_manager, 'trade_recorder', None)
    if not recorder:
        return []
        
    trades = await recorder.get_trades_for_strategy(strategy_id, limit)
    
    # Convert tuples to dicts for JSON response
    # Actual DB Schema finding: 
    # 0:id, 1:strategy_id, 2:instrument_id, 3:entry_time, 4:entry_price, 5:exit_time, 
    # 6:exit_price, 7:exit_reason, 8:trade_type, 9:quantity, 10:direction, 11:pnl, 
    # 12:raw_data, 13:commission, 14:result
    result = []
    for t in trades:
        # Safety check for tuple length in case of schema drifts
        t_len = len(t)
        trade_dict = {
            "id": t[0],
            "strategy_id": t[1],
            "instrument_id": t[2],
            "entry_time": t[3],
            "entry_price": t[4],
            "exit_time": t[5],
            "exit_price": t[6],
            "exit_reason": t[7],
            "trade_type": t[8],
            "quantity": t[9],
            "direction": t[10],
            "pnl": t[11],
            "raw_data": t[12] if t_len > 12 else None,
            "commission": t[13] if t_len > 13 else 0.0,
            "result": t[14] if t_len > 14 else None
        }
        result.append(trade_dict)
    return result

@app.get("/strategies/{strategy_id}/stats")
async def get_strategy_stats(strategy_id: str):
    if not nautilus_manager.strategy_manager:
        return {}
        
    recorder = getattr(nautilus_manager, 'trade_recorder', None)
    if not recorder:
        return {}
        
    return await recorder.get_strategy_stats(strategy_id)

# Strategy Management Endpoints

@app.get("/strategies")
async def list_strategies():
    if not nautilus_manager.strategy_manager:
        return []
    return await nautilus_manager.strategy_manager.get_all_strategies_status()

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

@app.post("/analytics/spx/start")
async def start_spx_stream():
    try:
        id = await nautilus_manager.start_spx_stream()
        return {"status": "started", "id": id}
    except Exception as e:
        logger.error(f"Error starting SPX stream: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/analytics/spx/stop")
async def stop_spx_stream():
    try:
        id = await nautilus_manager.stop_spx_stream()
        return {"status": "stopped", "id": id}
    except Exception as e:
        logger.error(f"Error stopping SPX stream: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/analytics/reports/{report_type}")
async def get_report(report_type: str):
    """
    Generate and retrieve a specific type of report (fills, orders, positions).
    """
    try:
        if report_type not in ["fills", "orders", "positions"]:
             raise HTTPException(status_code=400, detail="Invalid report type. options: fills, orders, positions")
             
        data = await nautilus_manager.get_generated_report(report_type)
        return data
    except Exception as e:
        logger.error(f"Error generating report: {e}")
        raise HTTPException(status_code=500, detail=str(e))
