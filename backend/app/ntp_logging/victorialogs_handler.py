# VictoriaLogs Handler for Python Logging
#
# Fire-and-forget async handler that pushes logs to VictoriaLogs via HTTP.
# CRITICAL: Trading must never block on logging.

import logging
import json
import queue
import threading
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import requests


class VictoriaLogsHandler(logging.Handler):
    """
    Async log handler that pushes to VictoriaLogs via JSON stream API.
    
    Design principles:
    - Fire-and-forget: logs are queued and pushed in background
    - Never blocks: if queue is full, logs are dropped silently
    - Fails silently: network errors don't affect trading
    
    VictoriaLogs best practices:
    - JSON line format for efficient ingestion
    - _stream_fields for low-cardinality grouping (strategy_id, source, level)
    - All other fields as regular indexed fields
    """
    
    def __init__(
        self, 
        victorialogs_url: str = "http://victorialogs:9428",
        stream_fields: tuple = ("strategy_id", "source", "level"),
        extra_fields: Optional[Dict[str, str]] = None,
        batch_size: int = 100,
        flush_interval: float = 1.0,
        queue_size: int = 10000,
    ):
        super().__init__()
        
        # Build ingestion URL with stream fields
        stream_fields_param = ",".join(stream_fields)
        self.ingest_url = (
            f"{victorialogs_url}/insert/jsonline"
            f"?_stream_fields={stream_fields_param}"
            f"&_time_field=_time"
            f"&_msg_field=_msg"
        )
        
        self.extra_fields = extra_fields or {}
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        
        self.queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._shutdown = False
        
        # Background thread for batching and pushing
        self.worker = threading.Thread(target=self._worker, daemon=True, name="VictoriaLogsWorker")
        self.worker.start()
    
    def emit(self, record: logging.LogRecord) -> None:
        """
        Non-blocking: drops logs if queue is full.
        Trading continues regardless.
        """
        try:
            # Extract strategy_id from logger name: "strategy.orb_15min_call_1"
            strategy_id = None
            source = "system"
            
            if record.name.startswith("strategy."):
                parts = record.name.split(".", 1)
                if len(parts) > 1:
                    strategy_id = parts[1]
                source = "strategy"
            elif record.name.startswith("nautilus"):
                source = "nautilus"
            
            # Build log entry following VictoriaLogs data model
            log_entry: Dict[str, Any] = {
                # Special fields
                "_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "_msg": record.getMessage(),
                
                # Stream fields (for partitioning)
                "level": record.levelname,
                "source": source,
                
                # Regular indexed fields
                "component": record.name,
                "filename": record.filename,
                "lineno": record.lineno,
                "funcname": record.funcName,
            }
            
            # Add strategy_id only if present (to avoid empty stream field)
            if strategy_id:
                log_entry["strategy_id"] = strategy_id
            
            # Add extra context fields
            log_entry.update(self.extra_fields)
            
            # Add exception info if present
            if record.exc_info and record.exc_info[0] is not None:
                import traceback
                log_entry["error_type"] = record.exc_info[0].__name__
                log_entry["error_traceback"] = "".join(traceback.format_exception(*record.exc_info))
            
            # Add any extra attributes from the log record
            if hasattr(record, "extra") and isinstance(record.extra, dict):
                for key, value in record.extra.items():
                    if key not in log_entry:
                        log_entry[key] = str(value) if not isinstance(value, (str, int, float, bool, type(None))) else value
            
            self.queue.put_nowait(log_entry)
            
        except queue.Full:
            # Drop log rather than block trading
            pass
        except Exception:
            # Never raise from logging - silently ignore any errors
            pass
    
    def _worker(self) -> None:
        """Background worker that batches and pushes logs to VictoriaLogs."""
        batch: list = []
        last_flush = datetime.now()
        
        while not self._shutdown:
            try:
                # Collect logs with timeout for batching
                try:
                    entry = self.queue.get(timeout=self.flush_interval)
                    batch.append(entry)
                except queue.Empty:
                    pass
                
                # Flush when batch is full or interval elapsed
                now = datetime.now()
                should_flush = (
                    len(batch) >= self.batch_size or 
                    (batch and (now - last_flush).total_seconds() >= self.flush_interval)
                )
                
                if should_flush:
                    self._push_batch(batch)
                    batch = []
                    last_flush = now
                    
            except Exception:
                # Log failures don't matter - clear batch and continue
                batch = []
        
        # Final flush on shutdown
        if batch:
            self._push_batch(batch)
    
    def _push_batch(self, batch: list) -> None:
        """
        Push batch to VictoriaLogs using JSON line format.
        Fails silently - logging must never affect trading.
        """
        if not batch:
            return
            
        try:
            # VictoriaLogs accepts newline-delimited JSON (ndjson)
            payload = "\n".join(json.dumps(entry) for entry in batch)
            
            requests.post(
                self.ingest_url,
                data=payload,
                headers={"Content-Type": "application/stream+json"},
                timeout=5.0,
            )
        except Exception:
            # VictoriaLogs down? Don't care. Trading continues.
            pass
    
    def close(self) -> None:
        """Graceful shutdown with final flush."""
        self._shutdown = True
        if self.worker.is_alive():
            self.worker.join(timeout=2.0)
        super().close()
