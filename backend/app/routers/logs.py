# Log Query API
#
# FastAPI endpoints for querying logs from VictoriaLogs.
# Provides guardrailed access for React UI with sensible defaults and limits.

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta
from typing import Optional, Annotated
import json
import os

import httpx

router = APIRouter(prefix="/api/logs", tags=["logs"])

VICTORIALOGS_URL = os.getenv("VICTORIALOGS_URL", "http://victorialogs:9428")
MAX_ENTRIES = 2000  # Guardrail: prevent memory explosion
MAX_RANGE_HOURS = 24  # Guardrail: prevent expensive full scans
QUERY_TIMEOUT = 10.0  # seconds


@router.get("/")
async def get_logs(
    strategy_id: Optional[str] = None,
    level: Optional[str] = None,
    source: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    search: Optional[str] = None,
    exclude_containers: Annotated[Optional[list[str]], Query()] = None,
    limit: Annotated[int, Query(le=MAX_ENTRIES)] = 100,
):
    """
    Query logs from VictoriaLogs with guardrails.
    
    Args:
        strategy_id: Filter by specific strategy
        level: Filter by log level (INFO, WARNING, ERROR, CRITICAL, DEBUG)
        source: Filter by source (strategy, system, nautilus, container)
        start: Start time (default: 1 hour ago)
        end: End time (default: now)
        search: Full-text search term
        exclude_containers: List of container names to exclude
        limit: Maximum entries to return (max 500)
    
    Returns:
        List of log entries, most recent first.
    """
    # Default to last 1 hour
    if not end:
        end = datetime.utcnow()
    if not start:
        start = end - timedelta(hours=1)
    
    # Guardrail: max 24h range
    if (end - start).total_seconds() > MAX_RANGE_HOURS * 3600:
        raise HTTPException(
            status_code=400, 
            detail=f"Time range exceeds {MAX_RANGE_HOURS}h limit"
        )
    
    # Build LogsQL query
    query_parts = []
    
    # Time filter (always first for performance)
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    query_parts.append(f"_time:[{start_iso}, {end_iso}]")
    
    # Stream field filters
    if strategy_id:
        query_parts.append(f'strategy_id:"{strategy_id}"')
    if source:
        query_parts.append(f'source:"{source}"')
    if level:
        query_parts.append(f"level:{level}")
    
    # Exclusions
    if exclude_containers:
        for container in exclude_containers:
            query_parts.append(f'container_name:!"{container}"')
    
    # Full-text search
    if search:
        query_parts.append(search)
    
    query = " ".join(query_parts)
    
    try:
        async with httpx.AsyncClient(timeout=QUERY_TIMEOUT) as client:
            resp = await client.get(
                f"{VICTORIALOGS_URL}/select/logsql/query",
                params={
                    "query": query,
                    "limit": limit,
                }
            )
            resp.raise_for_status()
            
            # VictoriaLogs returns newline-delimited JSON
            logs = []
            for line in resp.text.strip().split("\n"):
                if line:
                    try:
                        logs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            
            return {"logs": logs, "count": len(logs), "query": query}
            
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="VictoriaLogs query timeout")
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="VictoriaLogs unavailable")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"VictoriaLogs error: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"VictoriaLogs error: {str(e)}")


@router.get("/tail")
async def tail_logs(
    strategy_id: Optional[str] = None,
    source: Optional[str] = None,
    level: Optional[str] = None,
    exclude_containers: Annotated[Optional[list[str]], Query()] = None,
    search: Optional[str] = None,
    limit: Annotated[int, Query(le=MAX_ENTRIES)] = 50,
):
    """
    Get most recent logs (last 5 minutes).
    Optimized for React UI polling.
    """
    return await get_logs(
        strategy_id=strategy_id,
        source=source,
        level=level,
        exclude_containers=exclude_containers,
        search=search,
        start=datetime.utcnow() - timedelta(minutes=5),
        limit=limit,
    )


@router.get("/strategy/{strategy_id}")
async def get_strategy_logs(
    strategy_id: str,
    level: Optional[str] = None,
    start: Optional[datetime] = None,
    # end: Optional[datetime] = None, # Implicitly now
    search: Optional[str] = None,
    limit: Annotated[int, Query(le=MAX_ENTRIES)] = 100,
):
    """
    Convenience endpoint for strategy-specific logs.
    By default returns logs from the last hour, unless start is provided.
    """
    # If no start time provided, default to 24h ago per user request for history
    if not start:
        start = datetime.utcnow() - timedelta(hours=12)

    return await get_logs(
        strategy_id=strategy_id,
        source="strategy",
        level=level,
        search=search,
        start=start,
        limit=limit,
    )


@router.get("/errors")
async def get_recent_errors(
    strategy_id: Optional[str] = None,
    limit: Annotated[int, Query(le=MAX_ENTRIES)] = 50,
):
    """
    Get recent ERROR and CRITICAL logs from the last hour.
    """
    return await get_logs(
        strategy_id=strategy_id,
        level="ERROR",
        start=datetime.utcnow() - timedelta(hours=1),
        limit=limit,
    )


@router.get("/health")
async def logs_health():
    """
    Check if VictoriaLogs is reachable.
    Returns 200 if healthy, 502 if VictoriaLogs is down.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{VICTORIALOGS_URL}/health")
            if resp.status_code == 200:
                return {"status": "healthy", "victorialogs": "connected"}
    except Exception:
        pass
    
    raise HTTPException(status_code=502, detail="VictoriaLogs unavailable")
