"""Collector service: drives a BusinessDataSource with retries and pagination.

The collector knows nothing about HTTP, Excel or job storage.  It returns the
raw records it managed to collect plus an optional error.  Partial results are
kept even when an upstream page fails.
"""
import logging
import random
import time
from typing import Any, Callable, Optional, Tuple

from fastapi import HTTPException

from app.adapters.approved_source import BusinessDataSource

logger = logging.getLogger(__name__)

# 429-specific backoff: 1s, 2s, 4s, 8s, 15s, 30s
_429_BACKOFF = [1.0, 2.0, 4.0, 8.0, 15.0, 30.0]


def _retry(fetch: Callable, max_retries: int = 3, sleep: Callable[[float], None] = time.sleep) -> Any:
    """Call ``fetch`` up to ``max_retries`` times with exponential backoff.

    If the fetch raises an ``HTTPException`` with status 429, we back off
    using increasing delays rather than immediately failing.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            return fetch()
        except HTTPException as exc:  # noqa: BLE001 – 429 from upstream
            last_exc = exc
            logger.warning(
                "Upstream attempt %d/%d failed with 429: %s",
                attempt,
                max_retries,
                exc.detail,
            )
            if attempt < max_retries:
                # use 429-aware backoff with jitter
                delay = _429_BACKOFF[min(attempt, len(_429_BACKOFF) - 1)]
                sleep(delay * (1 + random.uniform(-0.1, 0.1)))
        except Exception as exc:  # noqa: BLE001 - we intentionally handle all upstream errors
            last_exc = exc
            logger.warning("Upstream attempt %d/%d failed: %s", attempt, max_retries, exc)
            if attempt < max_retries:
                delay = 2 ** (attempt - 1)
                sleep(delay)
    assert last_exc is not None
    raise last_exc


def collect(
    source: BusinessDataSource,
    query: str,
    location: str,
    radius_km: Optional[float] = None,
    max_results: Optional[int] = None,
    page_size: int = 20,
    max_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> Tuple[list[dict], Optional[Exception], dict]:
    """Collect records, page by page, with retry + partial-failure handling.

    Returns ``(records, error, meta)`` where ``error`` is ``None`` on full
    success and ``meta`` contains simple counters for logging.
    """
    records: list[dict] = []
    page_token: Optional[str] = None
    pages_fetched = 0
    error: Optional[Exception] = None

    while True:
        if max_results is not None and len(records) >= max_results:
            break
        remaining = None if max_results is None else max_results - len(records)
        size = page_size if remaining is None else min(page_size, remaining)
        if size <= 0:
            break

        def fetch():
            return source.search_page(
                query=query,
                location=location,
                radius_km=radius_km,
                page_size=size,
                page_token=page_token,
            )

        try:
            page_records, next_token = _retry(fetch, max_retries=max_retries, sleep=sleep)
        except Exception as exc:  # noqa: BLE001
            logger.error("Collection stopped after partial results: %s", exc)
            error = exc
            break

        pages_fetched += 1
        records.extend(page_records or [])
        logger.debug("Fetched page %d: %d records (total %d)", pages_fetched, len(page_records or []), len(records))

        page_token = next_token
        if next_token is None:
            break

    meta = {"pages_fetched": pages_fetched, "requested_max": max_results}
    return records, error, meta