"""Tests for the Apify Actor entrypoint."""
import asyncio
from pathlib import Path
import pytest

from app.actor import _sync_scrape_pipeline


def test_actor_pipeline_synthetic(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("USE_PLAYWRIGHT_SOURCE", "0")

    params = {
        "query": "dental clinic",
        "location": "Dharampeth, Nagpur",
        "max_results": 5,
        "export_excel": True,
        "use_playwright_source": False,
    }

    records, excel_file, notes = _sync_scrape_pipeline(params)

    assert len(records) == 5
    assert records[0].business_name is not None
    assert excel_file is not None
    assert Path(excel_file).exists()
    assert Path(excel_file).suffix == ".xlsx"


def test_actor_pipeline_no_excel(monkeypatch):
    monkeypatch.setenv("USE_PLAYWRIGHT_SOURCE", "0")

    params = {
        "query": "gym",
        "location": "Nagpur",
        "max_results": 3,
        "export_excel": False,
        "use_playwright_source": False,
    }

    records, excel_file, notes = _sync_scrape_pipeline(params)

    assert len(records) == 3
    assert excel_file is None
