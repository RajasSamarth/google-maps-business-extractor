"""Automatic area discovery via OpenStreetMap (Overpass API).

Given a city name, returns its suburb / neighbourhood names so one job can
sweep them all instead of relying on a hand-typed area list. Standard library
only. Any failure returns [] and the caller falls back to searching the
plain location as before.
"""
import json
import logging
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)

_CACHE: dict[str, tuple[float, list[str]]] = {}
_CACHE_TTL = 6 * 3600  # discovered area lists barely change; cache 6h


def extract_city(location: str) -> str:
    """Take the city part of a location ("Dharampeth, Nagpur" -> "Nagpur")."""
    return (location or "").split(",")[-1].strip()


def _bbox_via_nominatim(city: str) -> tuple[float, float, float, float] | None:
    """Resolve a city to an (south, west, north, east) box. None on failure."""
    try:
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"format": "json", "limit": 1, "q": city}
        )
        req = urllib.request.Request(url, headers={"User-Agent": "maps-extractor/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if not data:
            return None
        bb = data[0].get("boundingbox")  # [south, north, west, east] as strings
        s, n, w, e = float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])
        return (s, w, n, e)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nominatim lookup failed for %r: %s", city, exc)
        return None


def _query(bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox
    return (
        "[out:json][timeout:25];"
        "node[\"place\"~\"^(suburb|neighbourhood|quarter|hamlet|village|town)$\"]"
        f"({s},{w},{n},{e});"
        "out tags;"
    )


def _parse_names(payload: dict) -> list[str]:
    names: list[str] = []
    elements = payload.get("elements") if isinstance(payload, dict) else None
    for el in elements or []:
        tags = (el or {}).get("tags") or {}
        name = (tags.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def discover_areas(city: str, limit: int = 30, timeout: int = 8) -> list[str]:
    """Return up to ``limit`` area names for ``city`` (may be empty)."""
    city = (city or "").strip().replace('"', "")[:100]
    if not city or limit <= 0:
        return []
    now = time.time()
    hit = _CACHE.get(city.lower())
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1][:limit]
    names: list[str] = []
    bbox = _bbox_via_nominatim(city)
    if bbox is None:
        return []
    query = _query(bbox)
    for endpoint in OVERPASS_URLS:
        try:
            data = urllib.parse.urlencode({"data": query}).encode()
            req = urllib.request.Request(
                endpoint,
                data=data,
                headers={"User-Agent": "maps-extractor/1.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            names = _parse_names(payload)[:limit]
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Area discovery via %s failed: %s", endpoint, exc)
    # Only cache real answers: a transient outage caching [] would silently
    # disable auto-areas for hours.
    if names:
        _CACHE[city.lower()] = (now, names)
    else:
        logger.warning("Area discovery failed for %r on all mirrors", city)
    logger.info("Discovered %d areas for %r", len(names), city)
    return names
