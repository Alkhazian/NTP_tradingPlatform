from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
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

logging.basicConfig(
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(), # Keep stdout
        file_handler
    ]
)
logger = logging.getLogger(__name__)

# Ensure library loggers also use our file handler
for logger_name in ["uvicorn", "uvicorn.error", "nautilus_trader"]:
    l = logging.getLogger(logger_name)
    l.addHandler(file_handler)
    l.propagate = True
logger.info("--- Log Stream Initialized ---")

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
    
    async def start_nautilus():
        logger.info("Starting NautilusTrader Manager...")
        try:
            await nautilus_manager.start()
            logger.info("NautilusTrader started successfully")
        except Exception as e:
            logger.error(f"Failed to start NautilusTrader: {e}")
            
    asyncio.create_task(start_nautilus())
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
        
        await asyncio.sleep(10)

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
    # Schema: id, strategy_id, instrument_id, entry_time, entry_price, exit_time, exit_price, exit_reason, trade_type, quantity, direction, pnl, raw_data
    result = []
    for t in trades:
        result.append({
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
            "raw_data": t[12]
        })
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

# Backtesting Endpoints

from .backtest_manager import BacktestManager, BacktestConfig
from .services.data_ingestion import ParquetImporter
from fastapi.responses import FileResponse

# Initialize backtest manager and data importer
backtest_manager = BacktestManager()
data_importer = ParquetImporter()

@app.post("/backtest/run")
async def run_backtest(config_dict: dict):
    """Run a new backtest."""
    try:
        config = BacktestConfig(
            strategy_id=config_dict["strategy_id"],
            strategy_config=config_dict["strategy_config"],
            instruments=config_dict["instruments"],
            start_date=config_dict["start_date"],
            end_date=config_dict["end_date"],
            venue=config_dict.get("venue", "SIM"),
            initial_balance=config_dict.get("initial_balance", 100000.0),
            currency=config_dict.get("currency", "USD"),
        )
        # Commission and slippage are passed inside strategy_config or as direct fields
        # In this implementation, we added them to BacktestConfig's constructor by extracting from strategy_config
        
        result = await backtest_manager.run_backtest(config)
        return result
    except Exception as e:
        logger.error(f"Error running backtest: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/backtest/results/{run_id}")
async def get_backtest_results(run_id: str):
    """Get results for a specific backtest run."""
    results = backtest_manager.get_results(run_id)
    if not results:
        raise HTTPException(status_code=404, detail=f"Backtest run {run_id} not found")
    return results

@app.get("/backtest/results")
async def list_backtest_results():
    """List all backtest results."""
    return backtest_manager.list_results()

@app.get("/backtest/tearsheet/{run_id}")
async def get_tearsheet(run_id: str):
    """Download the HTML tearsheet for a backtest run."""
    results = backtest_manager.get_results(run_id)
    if not results:
        raise HTTPException(status_code=404, detail=f"Backtest run {run_id} not found")
    
    tearsheet_path = results.get("tearsheet_path")
    if not tearsheet_path or not os.path.exists(tearsheet_path):
        raise HTTPException(status_code=404, detail="Tearsheet not found")
    
    return FileResponse(
        tearsheet_path,
        media_type="text/html",
        filename=f"tearsheet_{run_id}.html"
    )

@app.get("/backtest/available-data")
async def get_available_data():
    """List all available data in the catalog."""
    return data_importer.list_available_data()

from fastapi import BackgroundTasks

# In-memory status tracking for ingestion (simplified for now)
ingestion_status = {
    "is_ingesting": False,
    "last_result": None,
    "current_file": None,
    "error": None
}

@app.get("/backtest/ingest-status")
async def get_ingest_status():
    """Get the status of the current or last ingestion task."""
    return ingestion_status

def run_ingestion_task(config: dict):
    """Background task for data ingestion."""
    global ingestion_status
    ingestion_status["is_ingesting"] = True
    ingestion_status["error"] = None
    ingestion_status["last_result"] = None
    
    try:
        if "file_path" in config:
            ingestion_status["current_file"] = config["file_path"]
            bars_count = data_importer.ingest_parquet_file(
                file_path=config["file_path"],
                instrument_id=config["instrument_id"],
                venue=config.get("venue", "SIM"),
                bar_type=config.get("bar_type", "1-MINUTE-LAST"),
                timezone=config.get("timezone", "UTC")
            )
            ingestion_status["last_result"] = {"status": "success", "bars_ingested": bars_count}
        elif "directory" in config:
            ingestion_status["current_file"] = config["directory"]
            results = data_importer.ingest_directory(
                directory=config["directory"],
                instrument_mapping=config["instrument_mapping"],
                venue=config.get("venue", "SIM"),
                timezone=config.get("timezone", "UTC"),
                bar_type=config.get("bar_type", "1-MINUTE-LAST")
            )
            ingestion_status["last_result"] = {"status": "success", "results": results}
    except Exception as e:
        logger.error(f"Background ingestion error: {e}", exc_info=True)
        ingestion_status["error"] = str(e)
    finally:
        ingestion_status["is_ingesting"] = False
        ingestion_status["current_file"] = None

@app.post("/backtest/create-instrument")
async def create_instrument(config: dict):
    """
    Create an instrument definition in the catalog.
    
    Expected config format:
    {
        "symbol": "MES",
        "instrument_type": "futures",
        "venue": "SIM",
        "multiplier": "5",
        "price_increment": "0.25"
    }
    """
    try:
        # Create a copy and remove explicit arguments to avoid "multiple values for keyword argument" error
        instrument_config = config.copy()
        symbol = instrument_config.pop("symbol")
        instrument_type = instrument_config.pop("instrument_type", "futures")
        venue = instrument_config.pop("venue", "SIM")
        
        instrument = data_importer.create_instrument_definition(
            symbol=symbol,
            instrument_type=instrument_type,
            venue=venue,
            **instrument_config
        )
        return {"status": "success", "instrument_id": str(instrument.id)}
    except Exception as e:
        logger.error(f"Error creating instrument: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/backtest/trades-csv/{run_id}")
async def export_trades_csv(run_id: str):
    """Export backtest trades to CSV file."""
    try:
        csv_path = backtest_manager.export_trades_to_csv(run_id)
        
        if not os.path.exists(csv_path):
            raise HTTPException(status_code=404, detail="CSV file not found")
        
        return FileResponse(
            csv_path,
            media_type="text/csv",
            filename=f"trades_{run_id}.csv"
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error exporting trades CSV: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


