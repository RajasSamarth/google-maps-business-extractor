"""Interfaces and implementations for business data sources.

The rest of the application depends only on ``BusinessDataSource`` (the methods
below).  Concrete adapters (synthetic today, an authorized live source later)
can be swapped without changing the API, cleaner, or exporter.
"""
from typing import Optional, Protocol, Tuple

# A page of raw records plus an opaque token for the next page (or None).
PageResult = Tuple[list[dict], Optional[str]]


class BusinessDataSource(Protocol):
    def search_page(
        self,
        query: str,
        location: str,
        radius_km: Optional[float] = None,
        page_size: int = 20,
        page_token: Optional[str] = None,
    ) -> PageResult:
        """Return one page of raw records and the next page token."""
        ...


class SyntheticDataSource:
    """A deterministic-ish in-memory source for development and tests.

    It simulates pagination, partial missing fields, and optional failures so the
    collector/retry logic can be exercised without a live website.
    """

    def __init__(
        self,
        total_available: int = 60,
        page_size: int = 20,
        fail_on_page: Optional[int] = None,
        fail_always: bool = False,
    ):
        self.total_available = max(0, total_available)
        self.page_size = max(1, page_size)
        self.fail_on_page = fail_on_page  # 1-based page number that raises
        self.fail_always = fail_always
        self._page_index = 0

    def search_page(self, query, location, radius_km=None, page_size=20, page_token=None):
        page_number = int(page_token) if page_token is not None else 1
        if self.fail_always:
            raise RuntimeError("simulated upstream failure")
        if self.fail_on_page is not None and page_number == self.fail_on_page:
            raise RuntimeError(f"simulated failure on page {page_number}")

        size = min(page_size, self.total_available - (page_number - 1) * self.page_size)
        # Guard against page_size mismatch; use requested size cap.
        size = max(0, min(size, page_size))
        records = []
        start = (page_number - 1) * self.page_size
        for i in range(size):
            n = start + i + 1
            rec = {
                "business_name": f"{query.title()} Result {n}",
                "category": query,
                "full_address": f"{n} Sample Street, {location}",
                "area_locality": location.split(",")[0].strip(),
                "city_pincode": "Nagpur 440010" if "Nagpur" in location else location,
                "phone_number": f"+91 90000{n:05d}" if n % 3 else None,
                "website": f"https://example.com/{n}" if n % 2 else None,
                "rating": round(3.5 + (n % 10) * 0.1, 1),
                "total_reviews": n * 3 if n % 2 else None,
                "opening_hours": "Mon-Sat 10:00-19:00" if n % 4 else None,
                "business_status": "Operational",
                "latitude": 21.1458 + (n * 0.0005),
                "longitude": 79.0882 + (n * 0.0005),
                "google_maps_link": f"https://maps.example/{n}",
                "place_id": f"place_{n}" if n % 2 else (f"place_dup" if n == 4 else None),
            }
            records.append(rec)

        total_pages = -(-self.total_available // self.page_size)  # ceil division
        next_token = str(page_number + 1) if page_number < total_pages and size > 0 else None
        return records, next_token
