# Google Maps Business Extractor

A FastAPI service that searches live Google Maps business listings via
Playwright browser automation and exports the results to an Excel file.
It ships with a built-in web frontend — open http://127.0.0.1:8000/ after
starting the server.

## Requirements

- Python 3.10+
- Windows / PowerShell (commands below)

## Setup (PowerShell)

```powershell
cd "D:\Velocity Projects\Google Scraper\google-maps-business-extractor"
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Copy `.env` and review the values:

```text
API_KEY=change-me-in-development
HOST=127.0.0.1
PORT=8000
FILE_RETENTION_HOURS=24
CACHE_TTL_HOURS=24
RATE_LIMIT_PER_MINUTE=30
MAX_JOBS=10
MAX_JOBS_PER_HOUR=10
```

## Run the API

```powershell
.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- Swagger UI: http://127.0.0.1:8000/docs
- ReDoc: http://127.0.0.1:8000/redoc
- Web frontend: http://127.0.0.1:8000/ (search, preview all results, download)

> **All results:** leave `max_results` empty (or omit it) to collect every
> listing Google Maps returns for the query — the scraper scrolls the full
> results feed and enriches each listing. Expect large searches to take a
> few minutes.

## Authentication

All endpoints require the header:

```text
X-API-Key: <value from .env API_KEY>
```

## Endpoints

### 1. Start a search

`POST /api/v1/search`

```json
{
  "query": "dental clinic",
  "location": "Dharampeth, Nagpur",
  "radius_km": 3,
  "max_results": 200,
  "min_rating": 4.0
}
```

Response:

```json
{
  "job_id": "…",
  "status": "queued",
  "status_url": "/api/v1/search/…"
}
```

### 2. Check status

`GET /api/v1/search/{job_id}`

Response (done):

```json
{
  "job_id": "…",
  "status": "done",
  "total_results": 87,
  "download_url": "/api/v1/search/…/download"
}
```

Statuses: `queued`, `running`, `done`, `failed`.

### 3. Preview results

`GET /api/v1/search/{job_id}/results`

Returns the cleaned records as JSON (used by the frontend preview table).

### 4. Download Excel

`GET /api/v1/search/{job_id}/download`

Returns the generated `.xlsx` file.

## Excel output

Workbook contains two sheets:

- `Businesses` with columns:
  `Sr. No., Business Name, Category, Full Address, Area / Locality, City / Pincode,
   Phone Number, Website, Rating, Total Reviews, Opening Hours, Business Status,
   Latitude, Longitude, Google Maps Link, Place ID`
- `Search Info` with `Search query, Location, Search radius, Search timestamp,
   Total result count`.

Filename pattern: `dental_clinic_dharampeth_nagpur_2026-10-14.xlsx`.

## Data source

The project is structured so the collector depends only on the
`BusinessDataSource` interface. Live Google Maps extraction is implemented in
`app/adapters/playwright_source.py` (Playwright/Chromium, enabled with
`USE_PLAYWRIGHT_SOURCE=1` — only use with explicit permission to automate
Google Maps). `SyntheticDataSource` (in `app/adapters/approved_source.py`)
remains available for development and testing with `USE_PLAYWRIGHT_SOURCE=0`.

## Testing

```powershell
.venv\Scripts\Activate.ps1
pytest
```

## Notes / limitations

- Job state and cache are in-memory and single-process.
- File cleanup and caching are local-development grade.
- Synthetic data is used until an authorized data source is integrated.