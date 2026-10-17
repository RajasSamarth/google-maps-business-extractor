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

## Deploying to Apify via GitHub & CI/CD

This project is configured to run as an **Apify Actor** with full GitHub Actions CI/CD automation.

### Apify Actor Features
- **Apify Dataset**: All scraped and cleaned business records are pushed directly into Apify's Default Dataset (downloadable as JSON, CSV, XML, Excel).
- **Key-Value Store**: Generates a formatted multi-sheet Excel file (`OUTPUT.xlsx`) and saves it directly to the run's Key-Value store.
- **Proxy Support**: Supports Apify Residential / Datacenter Proxies and custom proxies via the Apify UI input schema or `PLAYWRIGHT_PROXY` / `HTTP_PROXY`.
- **Apify Console UI**: Fully defined input schema in `.actor/input_schema.json` with user-friendly fields for search queries, locations, radius, ratings, and multi-area sweeps.

### Setup GitHub CI/CD Deployment

Whenever you push to the `main` branch, the workflow [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml) runs tests and deploys the Actor to Apify automatically.

#### 1. Retrieve your Apify API Token
1. Log in to [Apify Console](https://console.apify.com/).
2. Navigate to **Settings** -> [**Integrations**](https://console.apify.com/settings/integrations).
3. Copy your personal **API Token**.

#### 2. Configure GitHub Secrets
1. In your GitHub repository, open **Settings** -> **Secrets and variables** -> **Actions**.
2. Click **New repository secret**.
3. Name: `APIFY_TOKEN`
4. Value: `<your-apify-token>`
5. *(Optional)* Name: `APIFY_ACTOR_ID` (e.g. `your-username/google-maps-business-extractor` if you want to deploy to a specific existing Actor). If omitted, Apify CLI uses the name defined in `.actor/actor.json`.

#### 3. Trigger Deployment
- **Automatic:** Push any commit to the `main` branch.
  ```bash
  git add .
  git commit -m "Configure Apify Actor and CI/CD"
  git push origin main
  ```
- **Manual:** Go to **Actions** in your GitHub repository, select **Deploy Actor to Apify**, and click **Run workflow**.

### Alternative: Native Apify GitHub Integration
You can also connect GitHub directly in Apify:
1. In [Apify Console](https://console.apify.com/actors), click **Create new** -> **Web & Git**.
2. Select **GitHub repository** and authorize your repo.
3. Apify will automatically rebuild on every commit to `main`.

### Testing the Actor Locally

You can test the Apify Actor locally using python or the Apify CLI:

```powershell
.venv\Scripts\Activate.ps1

# Run with synthetic test source (no browser launch):
$env:USE_PLAYWRIGHT_SOURCE="0"
python -m app.actor

# Run with live Playwright scraper:
$env:USE_PLAYWRIGHT_SOURCE="1"
python -m app.actor
```

Local results will be saved in `storage/datasets/default` and `storage/key_value_stores/default/OUTPUT.xlsx`.

## Notes / limitations

- Job state and cache in the FastAPI server are in-memory and single-process.
- When running on Apify, state and storage are persisted natively by the Apify platform.