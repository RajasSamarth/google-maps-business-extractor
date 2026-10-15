"""Tests for the collector."""
import time

from app.adapters.approved_source import SyntheticDataSource
from app.services.collector import collect


def test_collect_aggregates_pages():
    source = SyntheticDataSource(total_available=55, page_size=20)
    records, error, meta = collect(source, "dentist", "Dharampeth, Nagpur", max_results=200, sleep=lambda _x: None)
    assert error is None
    assert len(records) == 55
    assert meta["pages_fetched"] == 3  # 20 + 20 + 15


def test_collect_respects_max_results():
    source = SyntheticDataSource(total_available=55, page_size=20)
    records, error, meta = collect(source, "dentist", "Dharampeth, Nagpur", max_results=25, sleep=lambda _x: None)
    assert error is None
    assert len(records) == 25


def test_collect_partial_failure_keeps_records():
    # Fail on page 2 after successfully collecting page 1.
    source = SyntheticDataSource(total_available=55, page_size=20, fail_on_page=2)
    records, error, meta = collect(source, "dentist", "Dharampeth, Nagpur", max_results=200, sleep=lambda _x: None)
    assert error is not None
    assert len(records) == 20  # page 1 preserved


def test_collect_total_failure_returns_empty():
    source = SyntheticDataSource(total_available=55, page_size=20, fail_always=True)
    records, error, meta = collect(source, "dentist", "Dharampeth, Nagpur", sleep=lambda _x: None)
    assert error is not None
    assert records == []


def test_retry_backoff_called():
    calls = {"n": 0}
    sleeps = []

    class FlakySource:
        def search_page(self, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("flaky")
            return ([{"business_name": "ok"}], None)

    records, error, meta = collect(FlakySource(), "q", "loc", sleep=lambda d: sleeps.append(d))
    assert error is None
    assert records == [{"business_name": "ok"}]
    # Two failures before success -> sleeps of 1s and 2s.
    assert sleeps == [1, 2]
