"""Apify Actor entry point for Google Maps Business Data Extractor.

This script executes when running as an Apify Actor on the Apify platform or
locally via the Apify CLI / python -m app.actor. It reads input from the Actor
input schema, runs the search/scraper pipeline, pushes listings to the Apify
Dataset, and optionally saves the formatted Excel file to the Key-Value Store.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import os
from pathlib import Path
from typing import Any, Tuple

from apify import Actor

from app.schemas import BusinessRecord, SearchInfo
from app.services.cleaner import clean_records
from app.services.collector import collect
from app.services.excel_exporter import export_to_excel

logger = logging.getLogger("apify.actor")


def _sync_scrape_pipeline(params: dict[str, Any]) -> Tuple[list[BusinessRecord], Path | None, list[str]]:
    """Synchronous pipeline executed in a worker thread.

    Running in a separate thread ensures Playwright's internal asyncio.run()
    does not conflict with Apify's main event loop.
    """
    query = (params.get("query") or "dental clinic").strip()
    location = (params.get("location") or "Dharampeth, Nagpur").strip()
    radius_km = params.get("radius_km")
    max_results = params.get("max_results")
    min_rating = params.get("min_rating")
    enrich_details = params.get("enrich_details", True)
    auto_areas = params.get("auto_areas", False)
    max_areas = params.get("max_areas") or 30
    extra_locations = params.get("extra_locations") or []
    extra_queries = params.get("extra_queries") or []
    export_excel_flag = params.get("export_excel", True)
    use_playwright = params.get("use_playwright_source", True)

    notes: list[str] = []

    # Choose data source
    if use_playwright:
        os.environ["USE_PLAYWRIGHT_SOURCE"] = "1"
        from app.adapters.playwright_source import PlaywrightGoogleMapsSource
        source = PlaywrightGoogleMapsSource(
            headless=True,
            target_cap=max_results,
            enrich=enrich_details,
        )
    else:
        from app.adapters.approved_source import SyntheticDataSource
        source = SyntheticDataSource(total_available=87, page_size=20)

    # Prepare queries
    queries = [query]
    for q in extra_queries:
        q_clean = (q or "").strip()
        if q_clean and q_clean not in queries:
            queries.append(q_clean)

    # Prepare locations
    locations = [location]
    for loc in extra_locations:
        loc_clean = (loc or "").strip()
        if loc_clean and loc_clean not in locations:
            locations.append(loc_clean)

    # Auto-discover areas if requested
    if auto_areas:
        from app.services.areas import discover_areas, extract_city

        city = extract_city(location)
        found = discover_areas(city, limit=max_areas)
        for area in found:
            label = area if not city or city.lower() in area.lower() else f"{area}, {city}"
            if label not in locations:
                locations.append(label)
        if not found:
            notes.append(f"Area auto-discovery for {city!r} returned no results; scraped main location.")

    all_raw: list[dict] = []
    page_error_notes: list[str] = []

    for q in queries:
        for loc in locations:
            page_records, page_err, _meta = collect(
                source=source,
                query=q,
                location=loc,
                radius_km=radius_km,
                max_results=None,
                page_size=20,
            )
            all_raw.extend(page_records or [])
            if page_err:
                page_error_notes.append(str(page_err))

    if page_error_notes:
        notes.append("; ".join(set(page_error_notes)))

    # Clean and deduplicate records
    cleaned = clean_records(all_raw, min_rating=min_rating)

    if max_results is not None and max_results > 0:
        cleaned = cleaned[:max_results]

    excel_file: Path | None = None
    if export_excel_flag:
        from datetime import timezone
        info = SearchInfo(
            query=", ".join(queries),
            location=", ".join(locations),
            radius_km=radius_km,
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            total_results=len(cleaned),
        )
        file_label = f"{locations[0]} + {len(locations) - 1} areas" if len(locations) > 1 else locations[0]
        output_dir = os.getenv("OUTPUT_DIR", "output")
        excel_file = export_to_excel(
            records=cleaned,
            search_info=info,
            output_dir=output_dir,
            filename_location=file_label,
        )

    return cleaned, excel_file, notes


async def main() -> None:
    """Actor execution entry point."""
    async with Actor:
        actor_input = await Actor.get_input() or {}
        Actor.log.info(f"Received Actor input: {actor_input}")

        # Set up proxy if configured
        proxy_input = actor_input.get("proxy_configuration")
        if proxy_input and (proxy_input.get("useApifyProxy") or proxy_input.get("proxyUrls")):
            proxy_config = await Actor.create_proxy_configuration(actor_proxy_input=proxy_input)
            if proxy_config:
                proxy_url = await proxy_config.new_url()
                if proxy_url:
                    os.environ["PLAYWRIGHT_PROXY"] = proxy_url
                    Actor.log.info("Configured proxy for browser requests.")
        else:
            os.environ.pop("PLAYWRIGHT_PROXY", None)
            Actor.log.info("Running with direct connection (no proxy).")

        # Default use_playwright_source to True unless explicitly disabled (or if mock testing)
        if "use_playwright_source" not in actor_input:
            actor_input["use_playwright_source"] = os.getenv("USE_PLAYWRIGHT_SOURCE", "1") == "1"

        Actor.log.info("Starting Google Maps extraction...")

        cleaned_records, excel_path, notes = await asyncio.to_thread(_sync_scrape_pipeline, actor_input)

        if notes:
            Actor.log.warning(f"Pipeline notes: {'; '.join(notes)}")

        # 1. Push records to Apify Default Dataset
        dataset_items = [r.to_dict() for r in cleaned_records]
        if dataset_items:
            await Actor.push_data(dataset_items)
            Actor.log.info(f"Successfully pushed {len(dataset_items)} listings to the Apify Dataset.")
        else:
            Actor.log.warning("No listings found matching the specified query and filters.")

        # 2. Push formatted Excel file to Key-Value Store if generated
        if excel_path and Path(excel_path).exists():
            with open(excel_path, "rb") as f:
                excel_bytes = f.read()

            excel_key = "OUTPUT.xlsx"
            await Actor.set_value(
                key=excel_key,
                value=excel_bytes,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            Actor.log.info(f"Formatted Excel workbook saved to Key-Value Store as '{excel_key}'.")

        await Actor.set_status_message(f"Scraped {len(cleaned_records)} listings successfully.")


if __name__ == "__main__":
    asyncio.run(main())
