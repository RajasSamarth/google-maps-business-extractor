"""Tests for automatic area discovery (no network: parsing + validation only)."""
from app.services.areas import _parse_names, discover_areas, extract_city


def test_extract_city():
    assert extract_city("Dharampeth, Nagpur") == "Nagpur"
    assert extract_city("Nagpur") == "Nagpur"
    assert extract_city("  Sadar, Nagpur  ") == "Nagpur"
    assert extract_city("") == ""


def test_parse_names_dedupes():
    payload = {"elements": [
        {"tags": {"name": "Sadar"}},
        {"tags": {"name": "Sadar"}},
        {"tags": {"name": "  Bajaj Nagar  "}},
        {"tags": {}},
        {},
    ]}
    assert _parse_names(payload) == ["Sadar", "Bajaj Nagar"]
    assert _parse_names({}) == []
    assert _parse_names(None) == []


def test_discover_short_circuits_without_network(monkeypatch):
    def _fail(*args, **kwargs):
        raise AssertionError("no network allowed")

    monkeypatch.setattr("urllib.request.urlopen", _fail)
    assert discover_areas("") == []
    assert discover_areas("   ") == []
    assert discover_areas("Nagpur", limit=0) == []


def test_discover_parses_response(monkeypatch):
    calls = {"n": 0}

    class _Resp:
        def __init__(self, body: bytes):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self._body

    def _fake(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # Nominatim bbox lookup
            return _Resp(b'[{"boundingbox": ["21.0", "21.2", "79.0", "79.2"]}]')
        # Overpass area lookup
        return _Resp(b'{"elements": [{"tags": {"name": "Sadar"}}, {"tags": {"name": "Itwari"}}]}')

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    assert discover_areas("Nagpur", limit=5) == ["Sadar", "Itwari"]
    assert discover_areas("Nagpur", limit=1) == ["Sadar"]


def test_discover_failure_returns_empty(monkeypatch):
    from app.services import areas as areas_mod

    def _boom(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    monkeypatch.setitem(areas_mod._CACHE, "offline city", (0.0, ["stale"]))
    assert discover_areas("Offline City") == []
