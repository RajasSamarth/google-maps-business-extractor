"""Basic API tests."""
import time
from fastapi.testclient import TestClient

from app.main import app

HEADERS = {"X-API-Key": "change-me-in-development"}
client = TestClient(app, headers=HEADERS)


def test_full_search_flow():
    resp = client.post("/api/v1/search", json={
        "query": "dental clinic",
        "location": "Dharampeth, Nagpur",
        "radius_km": 3,
        "max_results": 5,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    job_id = data["job_id"]

    # Wait for background job to finish.
    for _ in range(50):
        status_resp = client.get(f"/api/v1/search/{job_id}")
        assert status_resp.status_code == 200
        status_data = status_resp.json()
        if status_data["status"] in ("done", "failed"):
            break
        time.sleep(0.1)

    assert status_data["status"] == "done"
    assert status_data["total_results"] == 5
    assert "download_url" in status_data

    dl = client.get(f"/api/v1/search/{job_id}/download")
    assert dl.status_code == 200
    assert "spreadsheetml" in dl.headers["content-type"]
    assert len(dl.content) > 0


def test_unknown_job():
    resp = client.get("/api/v1/search/does-not-exist")
    assert resp.status_code == 404


def test_invalid_input():
    resp = client.post("/api/v1/search", json={"query": "", "location": "X"})
    assert resp.status_code == 400


def test_unauthorized():
    bad = TestClient(app)  # no API key
    resp = bad.post("/api/v1/search", json={"query": "gym", "location": "Dharampeth, Nagpur"})
    assert resp.status_code == 401
    resp = bad.get("/api/v1/search/anything")
    assert resp.status_code == 401


def test_download_unknown_job():
    resp = client.get("/api/v1/search/unknown/download")
    assert resp.status_code == 404


def test_index_is_public():
    anon = TestClient(app)  # no API key
    resp = anon.get("/")
    assert resp.status_code == 200
    assert "Google Maps Business Extractor" in resp.text


def test_results_endpoint_returns_all_records():
    resp = client.post("/api/v1/search", json={
        "query": "dental clinic",
        "location": "Dharampeth, Nagpur",
        "radius_km": 3,
        "max_results": 3,
    })
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    for _ in range(200):
        status_data = client.get(f"/api/v1/search/{job_id}").json()
        if status_data["status"] in ("done", "failed"):
            break
        time.sleep(1)

    assert status_data["status"] == "done"
    res = client.get(f"/api/v1/search/{job_id}/results")
    assert res.status_code == 200
    payload = res.json()
    assert payload["total_results"] == 3
    assert len(payload["records"]) == 3
    assert payload["records"][0]["business_name"]


def test_unauthorized_results():
    bad = TestClient(app)  # no API key
    resp = bad.get("/api/v1/search/anything/results")
    assert resp.status_code == 401


def test_multi_area_search_merges_areas():
    resp = client.post("/api/v1/search", json={
        "query": "restaurants",
        "location": "Area One",
        "extra_locations": ["Area Two"],
        "max_results": 100,
    })
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    for _ in range(50):
        status_data = client.get(f"/api/v1/search/{job_id}").json()
        if status_data["status"] in ("done", "failed"):
            break
        time.sleep(0.1)

    # Synthetic source holds 87 per area, but its fake place_ids repeat
    # across areas (odd n share place_{n}, plus one place_dup), so 8 of
    # Area Two's 13 overlap and dedupe out: 87 + 13 - 8 = 92.
    # Live place_ids are globally unique, so no such overlap occurs.
    assert status_data["status"] == "done"
    assert status_data["total_results"] == 92

    payload = client.get(f"/api/v1/search/{job_id}/results").json()
    assert len(payload["records"]) == 92
    addresses = " | ".join(r.get("full_address") or "" for r in payload["records"])
    assert "Area One" in addresses
    assert "Area Two" in addresses
