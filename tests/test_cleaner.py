"""Tests for the cleaner."""
from app.schemas import BusinessRecord
from app.services.cleaner import clean_records, normalize_record, deduplicate, filter_by_rating


def test_normalize_coerces_types():
    raw = {
        "business_name": "  Foo Clinic  ",
        "rating": "4.6 stars",
        "total_reviews": "1,234",
        "latitude": "21.14",
        "longitude": 79.08,
        "place_id": "p1",
    }
    rec = normalize_record(raw)
    assert rec.business_name == "Foo Clinic"
    assert rec.rating == 4.6
    assert rec.total_reviews == 1234
    assert rec.latitude == 21.14
    assert rec.longitude == 79.08
    assert rec.place_id == "p1"
    # Missing fields stay None
    assert rec.website is None


def test_deduplicate_by_place_id():
    recs = [
        BusinessRecord(business_name="A", place_id="p1"),
        BusinessRecord(business_name="A duplicate", place_id="p1"),
        BusinessRecord(business_name="B", place_id="p2"),
    ]
    out = deduplicate(recs)
    assert [r.business_name for r in out] == ["A", "B"]


def test_deduplicate_composite_without_place_id():
    recs = [
        BusinessRecord(business_name="Clinic X", full_address="Addr 1"),
        BusinessRecord(business_name="clinic x", full_address="addr 1"),  # duplicate
        BusinessRecord(business_name="Clinic Y", full_address="Addr 2"),
    ]
    out = deduplicate(recs)
    assert [r.business_name for r in out] == ["Clinic X", "Clinic Y"]


def test_filter_by_rating():
    recs = [
        BusinessRecord(business_name="A", rating=4.0),
        BusinessRecord(business_name="B", rating=3.0),
        BusinessRecord(business_name="C"),  # no rating kept? filter considers None as unknown -> kept
    ]
    out = filter_by_rating(recs, 3.5)
    assert [r.business_name for r in out] == ["A", "C"]


def test_clean_records_end_to_end():
    raw = [
        {"business_name": "Clinic A", "rating": "5.0", "place_id": "p1"},
        {"business_name": "Clinic A copy", "rating": "4.0", "place_id": "p1"},  # dup
        {"business_name": "Clinic B", "rating": "2.5", "place_id": "p2"},
    ]
    out = clean_records(raw, min_rating=3.0)
    assert len(out) == 1
    assert out[0].business_name == "Clinic A"
    assert out[0].rating == 5.0
