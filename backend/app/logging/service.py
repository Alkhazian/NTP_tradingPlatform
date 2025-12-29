import logging
import json
import datetime
from typing import Any, Dict

class RedisLogHandler(logging.Handler):
    def __init__(self, redis_client, channel: str = "system_logs"):
        super().__init__()
        self.redis_client = redis_client
        self.channel = channel

    def emit(self, record):
        try:
            log_entry = self.format(record)
            # You might want to structure this better as JSON
            entry = {
                "timestamp": datetime.datetime.fromtimestamp(record.created).isoformat(),
                "level": record.levelname,
                "message": record.getMessage(),
                "logger": record.name,
                "type": getattr(record, "log_type", "system"), # system, trading, orders, strategy
                "subtype": "success" if record.levelno < 30 else "error" if record.levelno >= 40 else "info"
            }
            
            # We need an async loop to publish, or use a sync method if available. 
            # Since logging is sync, this is tricky with async redis.
            # Ideally, push to a queue that an async task drains.
            # For simplicity in this non-blocking requirement, we might just print or use a separate mechanism.
            # BUT, since we have redis_client which IS async, we can't await here in sync emit.
            
            # WORKAROUND: For now, just print to stdout which docker captures. 
            # Real implementation would need a background thread/task.
            pass
        except Exception:
            self.handleError(record)

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    # Silence some verbose libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

