"""Very small in-memory rate limiter with 429-aware backoff (development grade)."""
import os
import random
import threading
import time
from typing import Dict, List

from fastapi import HTTPException, Request

_lock = threading.Lock()
_hits: Dict[str, List[float]] = {}
_job_hits: Dict[str, List[float]] = {}


def _random_jitter(base: float) -> float:
    """Add a small random +/- 10% jitter to a backoff delay."""
    return base * (1 + random.uniform(-0.1, 0.1))


def check_rate_limit(request: Request) -> None:
    """Check per-minute rate limit for API requests."""
    limit = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
    window = 60.0
    client_id = request.client.host if request.client else "unknown"
    now = time.time()
    with _lock:
        recent = [t for t in _hits.get(client_id, []) if now - t < window]
        if len(recent) >= limit:
            oldest = min(recent)
            wait = max(0, window - (now - oldest))
            jittered = _random_jitter(wait)
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded. Try again in {int(jittered)} seconds.",
            )
        recent.append(now)
        _hits[client_id] = recent


def check_job_quota(request: Request) -> None:
    """Cap how many search *jobs* each client may create per hour.

    A limit of 0 (or negative) means *unlimited*. The default `.env` ships
    with 0 so testing is never blocked; set a positive number to re-enable.
    """
    limit = int(os.getenv("MAX_JOBS_PER_HOUR", "0"))
    if limit <= 0:
        return
    window = 3600.0
    client_id = request.client.host if request.client else "unknown"
    now = time.time()
    with _lock:
        recent = [t for t in _job_hits.get(client_id, []) if now - t < window]
        if len(recent) >= limit:
            oldest = min(recent)
            wait = max(0, window - (now - oldest))
            jittered = _random_jitter(wait)
            raise HTTPException(
                status_code=429,
                detail=f"Hourly job limit exceeded. Try again in {int(jittered // 60)} minutes.",
            )
        recent.append(now)
        _job_hits[client_id] = recent