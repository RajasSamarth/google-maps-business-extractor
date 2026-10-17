"""Tests for the Apify Actor entrypoint."""
import asyncio
from pathlib import Path
import pytest

from app.actor import _sync_scrape_pipeline
from app.adapters.playwright_source import _parse_proxy


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


def test_parse_proxy_credentials():
    url = "http://auto:secretpassword@proxy.apify.com:8000"
    cfg = _parse_proxy(url)
    assert cfg is not None
    assert cfg["server"] == "http://proxy.apify.com:8000"
    assert cfg["username"] == "auto"
    assert cfg["password"] == "secretpassword"


def test_parse_proxy_no_credentials():
    url = "http://myproxy.local:8080"
    cfg = _parse_proxy(url)
    assert cfg is not None
    assert cfg["server"] == "http://myproxy.local:8080"
    assert "username" not in cfg
    assert "password" not in cfg


def test_parse_proxy_empty():
    assert _parse_proxy("") is None
    assert _parse_proxy("   ") is None
