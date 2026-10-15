"""In-memory job tracking for search requests.

This is intentionally simple (single-process, memory only).  It is fine for
local development and demos; a production deployment should replace it with a
persistent store plus a real task queue.
"""
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import os
import time

from pathlib import Path

# already imports Path

def _output_dir() -> str:
    return os.getenv("OUTPUT_DIR", "output")


def cleanup_expired_files(output_dir: str | None = None, max_age_hours: int | None = None) -> int:
    """Delete generated Excel files older than max_age_hours. Returns count deleted."""
    if output_dir is None:
        output_dir = _output_dir()
    if max_age_hours is None:
        max_age_hours = int(os.getenv("FILE_RETENTION_HOURS", "24"))
    directory = Path(output_dir)
    if not directory.exists():
        return 0
    cutoff = time.time() - (max_age_hours * 3600)
    deleted = 0
    for file_path in directory.glob("*.xlsx"):
        try:
            if file_path.stat().st_mtime < cutoff:
                file_path.unlink()
                deleted += 1
        except OSError:
            continue
    return deleted


from typing import Any, Optional

from app.adapters.approved_source import SyntheticDataSource
from app.services.cleaner import clean_records
from app.services.collector import collect
from app.services.excel_exporter import export_to_excel

# ---- Job store ----
_JOBS: dict[str, "Job"] = {}
_LOCK = threading.Lock()

# Simple in-memory cache of finished jobs keyed by a normalized params hash.
_CACHE: dict[str, str] = {}  # cache_key -> job_id
_CACHE_EXPIRY: dict[str, datetime] = {}

# Guardrails (overridable via env at import if desired).
import logging
import time
from concurrent.futures import ThreadPoolExecutor

# at top? jobs already has imports; add after docstring? We'll just use module global. Need insert logger after imports. But imports typing already. We'll add `logger` after Job class? Simpler replace 'MAX_JOBS = 10' with logger definition.
MAX_JOBS = 10
logger = logging.getLogger(__name__)

# Dedicated pool so long scrapes never block the API event loop / threadpool.
_JOB_WORKERS = max(1, int(os.getenv("JOB_WORKERS", "3") or "3"))
_EXECUTOR = ThreadPoolExecutor(max_workers=_JOB_WORKERS, thread_name_prefix="search-job")

# -----------------------------------------------------------------
# Factory: return the concrete DataSource the job will use.
# -----------------------------------------------------------------
def _data_source(max_results: int | None = None, enrich: bool = True):
    """Pick a source based on environment variables.

    * ``USE_PLAYWRIGHT_SOURCE`` = "1"  → PlaywrightGoogleMapsSource (requires
      explicit organizational permission).
    * anything else → SyntheticDataSource (used for development / demos).
    """
    if __import__("os").getenv("USE_PLAYWRIGHT_SOURCE") == "1":
        # Lazy import so hosts without Playwright installed can still run
        # the synthetic demo source. Playwright is an optional dependency.
        from app.adapters.playwright_source import PlaywrightGoogleMapsSource
        # headless=True keeps the browser invisible; set False for debugging.
        # target_cap ensures a max_results=5 job scrapes ~5 listings, not 500.
        return PlaywrightGoogleMapsSource(headless=True, target_cap=max_results, enrich=enrich)
    return SyntheticDataSource(total_available=87, page_size=20)


@dataclass
class Job:
    job_id: str
    params: dict[str, Any]
    status: str = "queued"
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    records: list = field(default_factory=list)
    total_results: int = 0
    file_path: Optional[str] = None
    error: Optional[str] = None


def _cache_key(params: dict[str, Any]) -> str:
    return repr(sorted((k, v) for k, v in params.items()))


def can_accept_new_job() -> bool:
    active = sum(1 for j in _JOBS.values() if j.status in ("queued", "running"))
    return active < MAX_JOBS


def create_job(params: dict[str, Any]) -> Job:
    """Create a (possibly cached) job and return it."""
    key = _cache_key(params)
    now = datetime.utcnow()

    # Cache hit: reuse the previous finished job if still fresh.
    cached_id = _CACHE.get(key)
    if cached_id and cached_id in _JOBS:
        expiry = _CACHE_EXPIRY.get(key)
        if expiry is None or expiry > now:
            cached_file = _JOBS[cached_id].file_path
            if cached_file and Path(cached_file).exists():
                return _JOBS[cached_id]

    job = Job(job_id=str(uuid.uuid4()), params=params)
    with _LOCK:
        _JOBS[job.job_id] = job
    return job


# Fix attribute access: keep a plain helper.
def get_job(job_id: str) -> Optional[Job]:
    with _LOCK:
        return _JOBS.get(job_id)


def list_job_ids() -> list[str]:
    with _LOCK:
        return list(_JOBS.keys())


def run_job(job_id: str) -> None:
    """Execute a job synchronously. Designed to run in a background task."""
    job = get_job(job_id)
    if job is None:
        return

    job.status = "running"
    job.started_at = datetime.utcnow()
    started_at_monotonic = time.monotonic()
    params = job.params
    logger.info("Job %s running params=%s", job_id, params)
    try:
        # For now we use the synthetic approved source. Swap this for the real
        # adapter once the data source has been authorized.
        source = _data_source(max_results=params.get("max_results"),
                              enrich=params.get("enrich_details", True))

        radius = params.get("radius_km")
        max_results = params.get("max_results")
        min_rating = params.get("min_rating")

        # Fan-out: every query variant x every area, each scraped as its own
        # feed (Google caps a single feed at ~120 listings), then merged
        # and deduped into one result set.
        queries = [params["query"]]
        for extra in params.get("extra_queries") or []:
            extra = (extra or "").strip()
            if extra and extra not in queries:
                queries.append(extra)
        locations = [params["location"]]
        for extra in params.get("extra_locations") or []:
            extra = (extra or "").strip()
            if extra and extra not in locations:
                locations.append(extra)

        # Automatic area-wise sweep: discover the city's localities and add
        # them, so "restaurants in Nagpur" fans out without a hand-typed list.
        discovery_note = None
        if params.get("auto_areas"):
            from app.services.areas import discover_areas, extract_city

            city = extract_city(params["location"])
            found = discover_areas(city, limit=params.get("max_areas") or 30)
            for area in found:
                label = area if not city or city.lower() in area.lower() else f"{area}, {city}"
                if label not in locations:
                    locations.append(label)
            logger.info("Job %s auto-areas for %r: %d locations total", job_id, city, len(locations))
            if not found:
                discovery_note = f"Area auto-discovery for {city!r} failed; searched main location only."

        all_raw: list = []
        error = None
        for q in queries:
            for loc in locations:
                remaining = None if max_results is None else max_results - len(all_raw)
                if remaining is not None and remaining <= 0:
                    break
                page_records, page_error, _meta = collect(
                    source=source,
                    query=q,
                    location=loc,
                    radius_km=radius,
                    max_results=remaining,
                    page_size=20,
                )
                all_raw.extend(page_records or [])
                if page_error is not None:
                    error = page_error
                logger.info("Job %s %r @ %r: %d records (total %d)", job_id, q, loc, len(page_records or []), len(all_raw))
            if remaining is not None and remaining <= 0:
                break

        records = all_raw

        cleaned = clean_records(records, min_rating=min_rating)

        from app.schemas import SearchInfo

        info = SearchInfo(
            query=", ".join(queries),
            location=", ".join(locations),
            radius_km=radius,
            timestamp=datetime.utcnow().isoformat(timespec="seconds"),
            total_results=len(cleaned),
        )

        # Keep records on the job BEFORE exporting, so a failed export
        # (e.g. an over-long file name) still leaves previewable results.
        job.records = cleaned
        job.total_results = len(cleaned)

        if len(locations) > 1:
            file_label = f"{locations[0]} + {len(locations) - 1} areas"
        else:
            file_label = locations[0]
        file_path = export_to_excel(cleaned, info, output_dir=_output_dir(),
                                    filename_location=file_label)
        job.file_path = str(file_path)

        # Cache the finished result for 24 hours.
        key = _cache_key(params)
        _CACHE[key] = job.job_id
        _CACHE_EXPIRY[key] = datetime.utcnow() + timedelta(hours=24)

        if error is not None and not cleaned:
            job.status = "failed"
            job.error = str(error)
        else:
            job.status = "done"
            notes = []
            if error is not None:
                notes.append(f"Partial failure: {error}")
            if discovery_note:
                notes.append(discovery_note)
            job.error = "; ".join(notes) if notes else None
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.error = str(exc)
    finally:
        job.finished_at = datetime.utcnow()
        duration = time.monotonic() - started_at_monotonic
        logger.info(
            "Job %s finished status=%s total=%d duration=%.2fs error=%s",
            job.job_id, job.status, job.total_results, duration, job.error,
        )


def submit_job(job_id: str) -> None:
    """Queue run_job on the dedicated pool. Returns immediately."""
    _EXECUTOR.submit(run_job, job_id)
