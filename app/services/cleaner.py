"""Cleaning and normalization helpers for business records.

The collector (when implemented) may return messy dictionaries coming from an
upstream source.  These helpers turn them into the canonical ``BusinessRecord``
format, remove duplicates and apply filters.  Nothing here talks to a network
or a file system.
"""
from typing import Any, Iterable, Optional

from app.schemas import BusinessRecord


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text == "":
        return None
    # Handle values like "4.6 stars" or "4,6"
    text = text.replace(",", ".").split()[0]
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if text == "":
        return None
    # Remove thousand separators commonly found in review counts.
    text = text.replace(",", "").replace(" ", "")
    try:
        return int(float(text))
    except ValueError:
        return None


def _to_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text != "" else None


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "skipped", "failed")
    return False


def normalize_record(raw: Any) -> BusinessRecord:
    """Convert a raw dict / BusinessRecord into a canonical BusinessRecord."""
    if isinstance(raw, BusinessRecord):
        # Re-normalize through a dict so types are coerced consistently.
        raw = raw.to_dict()

    raw = raw or {}
    return BusinessRecord(
        business_name=_to_str(raw.get("business_name")),
        category=_to_str(raw.get("category")),
        full_address=_to_str(raw.get("full_address")),
        area_locality=_to_str(raw.get("area_locality")),
        city_pincode=_to_str(raw.get("city_pincode")),
        phone_number=_to_str(raw.get("phone_number")),
        website=_to_str(raw.get("website")),
        rating=_to_float(raw.get("rating")),
        total_reviews=_to_int(raw.get("total_reviews")),
        opening_hours=_to_str(raw.get("opening_hours")),
        business_status=_to_str(raw.get("business_status")),
        latitude=_to_float(raw.get("latitude")),
        longitude=_to_float(raw.get("longitude")),
        google_maps_link=_to_str(raw.get("google_maps_link")),
        place_id=_to_str(raw.get("place_id")),
        detail_failed=_to_bool(raw.get("detail_failed")),
    )


def deduplicate(records: Iterable[BusinessRecord]) -> list[BusinessRecord]:
    """Remove duplicates using Place ID when available.

    Fallback order: place_id -> google_maps_link -> composite key.
    The old (name, address) fallback collapsed same-name chain branches
    in fast list-only mode where full_address is None (every "Domino's"
    became one record). The composite now includes link/coords/phone so
    only truly identical listings collapse.
    """
    seen_place_ids: set[str] = set()
    seen_composite: set[str] = set()
    result: list[BusinessRecord] = []
    for rec in records:
        rec = normalize_record(rec)
        if rec.place_id:
            if rec.place_id in seen_place_ids:
                continue
            seen_place_ids.add(rec.place_id)
        else:
            # Identity key only: name + address + phone + coords + link.
            # area_locality / category / website are sweep metadata or
            # chain-shared and must NOT distinguish listings.
            key = (
                (rec.business_name or "").strip().lower(),
                (rec.full_address or "").strip().lower(),
                (rec.phone_number or "").strip().lower(),
                str(rec.latitude or ""),
                str(rec.longitude or ""),
                (rec.google_maps_link or "").strip(),
            )
            # A bare name alone (fast mode with no address/coords/link)
            # must never collapse branches — keep it.
            if not key[0]:
                result.append(rec)
                continue
            has_signal = any(key[1:])
            if has_signal and key in seen_composite:
                continue
            if has_signal:
                seen_composite.add(key)
        result.append(rec)
    return result


def filter_by_rating(records: Iterable[BusinessRecord], min_rating: Optional[float]) -> list[BusinessRecord]:
    if min_rating is None:
        return list(records)
    try:
        threshold = float(min_rating)
    except (TypeError, ValueError):
        return list(records)
    out = []
    for rec in records:
        rec = normalize_record(rec)
        if rec.rating is None or rec.rating >= threshold:
            out.append(rec)
    return out


def clean_records(records: Iterable, min_rating: Optional[float] = None) -> list[BusinessRecord]:
    """Normalize, deduplicate, and (optionally) filter by minimum rating."""
    normalized = [normalize_record(r) for r in (records or [])]
    deduped = deduplicate(normalized)
    return filter_by_rating(deduped, min_rating)
