"""FastAPI application exposing the search / status / download endpoints."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.auth import require_api_key
from app.jobs import cleanup_expired_files, create_job, get_job, submit_job, can_accept_new_job
from app.rate_limit import check_rate_limit
from app.schemas import SearchCreatedResponse, SearchRequest, SearchStatusResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    removed = cleanup_expired_files()
    logging.getLogger(__name__).info("Startup cleanup removed %d expired files", removed)
    yield


app = FastAPI(
    title="Dharampeth Business Data Extractor",
    version="0.2.0",
    description="Extract business listings and export them to Excel.",
    lifespan=lifespan,
)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Auth + rate limiting applied per API route (keeps the frontend page public).
_api_deps = [Depends(require_api_key), Depends(check_rate_limit)]


@app.get("/", include_in_schema=False)
def index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(path=str(index_file), media_type="text/html")
    return JSONResponse(
        content={"message": "Dharampeth Business Data Extractor API. See /docs."}
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    messages = [err.get("msg", "invalid input") for err in exc.errors()]
    detail = "; ".join(messages) if messages else "Invalid input."
    return JSONResponse(status_code=400, content={"detail": detail})


@app.post("/api/v1/search", response_model=SearchCreatedResponse, dependencies=_api_deps)
def start_search(request: SearchRequest, background_tasks: BackgroundTasks):
    if not can_accept_new_job():
        raise HTTPException(status_code=429, detail="Too many active search jobs. Try again later.")

    params = request.model_dump()
    job = create_job(params)

    if job.status == "queued":
        # submit_job hands off to a dedicated thread pool and returns
        # instantly, so POST /search stays ~10ms even during a live scrape.
        background_tasks.add_task(submit_job, job.job_id)
        logging.getLogger(__name__).info("Started search job %s params=%s", job.job_id, params)

    return SearchCreatedResponse(
        job_id=job.job_id,
        status=job.status,
        status_url=f"/api/v1/search/{job.job_id}",
    )


@app.get("/api/v1/search/{job_id}", response_model=SearchStatusResponse, dependencies=_api_deps)
def search_status(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id.")

    download_url = None
    if job.status == "done" and job.file_path:
        download_url = f"/api/v1/search/{job_id}/download"

    return SearchStatusResponse(
        job_id=job.job_id,
        status=job.status,
        total_results=job.total_results if job.status in ("done", "failed") else None,
        download_url=download_url,
        error=job.error,
    )


@app.get("/api/v1/search/{job_id}/results", dependencies=_api_deps)
def search_results(job_id: str):
    """Return the cleaned business records for a finished job (for preview)."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id.")
    if job.status not in ("done", "failed"):
        raise HTTPException(status_code=409, detail="Search is not finished yet.")

    records = []
    for rec in job.records or []:
        if hasattr(rec, "to_dict"):
            records.append(rec.to_dict())
        elif isinstance(rec, dict):
            records.append(rec)

    return {
        "job_id": job.job_id,
        "status": job.status,
        "total_results": job.total_results,
        "records": records,
    }


@app.get("/api/v1/search/{job_id}/download", dependencies=_api_deps)
def download_result(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id.")
    if not job.file_path or not Path(job.file_path).exists():
        raise HTTPException(status_code=404, detail="Result file not available.")
    if job.status not in ("done", "failed"):
        raise HTTPException(status_code=409, detail="Search is not finished yet.")

    if job.status == "done":
        logging.getLogger(__name__).info("Download requested for job %s", job_id)

    return FileResponse(
        path=job.file_path,
        filename=Path(job.file_path).name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
