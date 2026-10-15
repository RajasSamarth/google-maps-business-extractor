"""Playwright-based Google Maps adapter (live data).

Drives a real Chromium browser to https://www.google.com/maps, searches
``query + location``, scrolls the results feed (infinite scroll) and — for
each listing — opens the detail pane to extract phone, website, address,
hours, rating, lat/lng, etc.

Only use after explicit organizational permission to automate Google Maps.
Polite pauses + per-minute rate limiting are built in; no CAPTCHA bypass.
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
from typing import List, Optional, Tuple
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.adapters.approved_source import BusinessDataSource

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

# ---- module-level rate limiting + list cache -------------------------------
_request_timestamps: list[float] = []
_MAX_PER_MINUTE = 30

_LIST_CACHE: dict[tuple[str, str], tuple[float, list[dict], bool]] = {}
_LIST_CACHE_TTL = 600.0  # 10 minutes


def _respect_rate_limit() -> None:
    now = time.time()
    global _request_timestamps
    _request_timestamps = [t for t in _request_timestamps if now - t < 60.0]
    if len(_request_timestamps) >= _MAX_PER_MINUTE:
        wait = 60.0 - (now - min(_request_timestamps)) + 1.0
        time.sleep(max(0.0, wait) * (1 + random.uniform(-0.1, 0.1)))
    _request_timestamps.append(time.time())


def _parse_reviews(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"[\d,]+", text.replace("\u00a0", " "))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _parse_rating(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"\d(?:[.,]\d)?", text.replace("\u00a0", " "))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def _parse_latlng(url: str) -> tuple[Optional[float], Optional[float]]:
    if not url:
        return None, None
    # detail URLs contain !3d<lat>!4d<lng>
    m = re.search(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)", url)
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except ValueError:
            pass
    # fallback: @lat,lng,
    m = re.search(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", url)
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except ValueError:
            pass
    return None, None


class PlaywrightGoogleMapsSource(BusinessDataSource):
    headless: bool = True
    pause_sec: float = 0.6
    max_per_page: int = 20

    def __init__(
        self,
        headless: bool = True,
        pause_sec: float | None = None,
        max_per_page: int = 20,
        target_cap: int | None = None,
        scroll_delay: float | None = None,
        detail_pause: float | None = None,
    ) -> None:
        self.headless = headless
        self.pause_sec = pause_sec if pause_sec is not None else _env_float("PLAYWRIGHT_PAUSE_SEC", 0.6)
        self.max_per_page = max_per_page
        # Total listings this job wants (None = all). Caps the scrape so a
        # max_results=5 job doesn't pay for a 500-listing scrape.
        if target_cap is None:
            _cap = os.getenv("PLAYWRIGHT_MAX_RESULTS_CAP", "")
            try:
                target_cap = int(_cap) if _cap.strip() else None
            except ValueError:
                target_cap = None
        self.target_cap = target_cap
        self.scroll_delay = scroll_delay if scroll_delay is not None else _env_float("PLAYWRIGHT_SCROLL_DELAY", 0.6)
        self.detail_pause = detail_pause if detail_pause is not None else _env_float("PLAYWRIGHT_DETAIL_PAUSE", 0.4)
        # Generous timeouts: free-tier containers render slowly; a timed-out
        # click used to drop the whole card (see enrich loop below).
        self.click_timeout = _env_int("PLAYWRIGHT_CLICK_TIMEOUT", 8000)
        self.detail_timeout = _env_int("PLAYWRIGHT_DETAIL_TIMEOUT", 8000)

    def _pause(self, mult: float = 1.0) -> None:
        time.sleep(self.pause_sec * mult)

    # -- public protocol ----------------------------------------------------
    def search_page(
        self,
        query: str,
        location: str,
        radius_km: Optional[float] = None,
        page_size: int = 20,
        page_token: Optional[str] = None,
    ) -> Tuple[List[dict], Optional[str]]:
        _respect_rate_limit()
        try:
            start = int(page_token) if page_token else 0
        except (TypeError, ValueError):
            start = 0
        needed = start + max(1, page_size)

        attempt = 0
        last_exc: Optional[Exception] = None
        while attempt < 3:
            try:
                full = self._get_or_scrape(query, location, needed)
                chunk = full[start:start + page_size]
                nxt = str(start + len(chunk)) if (start + len(chunk)) < len(full) else None
                return chunk, nxt
            except PlaywrightError as exc:
                last_exc = exc
                attempt += 1
                logger.warning("Playwright attempt %d/3 failed: %s", attempt, exc)
                _LIST_CACHE.pop((query.lower().strip(), location.lower().strip()), None)
                time.sleep(2 ** (attempt - 1))
        raise last_exc  # type: ignore[misc]

    # -- cache wrapper ------------------------------------------------------
    def _get_or_scrape(self, query: str, location: str, needed: int) -> list[dict]:
        key = (query.lower().strip(), location.lower().strip())
        now = time.time()
        full_cap = _env_int("PLAYWRIGHT_FULL_CAP", 500)
        if self.target_cap:
            target = max(needed, self.target_cap)
        else:
            # Uncapped ("give me all") job: scrape to the full cap.
            # `needed` is only the current page size (20) and must NOT size
            # the whole scrape, or every uncapped search stops at 20.
            target = min(full_cap, 500)
        hit = _LIST_CACHE.get(key)
        # A cached list is reusable only if it is fresh AND complete enough:
        # either its feed was fully exhausted, or it already holds everything
        # this job could want. Otherwise an old capped scrape would poison a
        # later uncapped ("give me all") search with stale partial data.
        if hit and (now - hit[0]) < _LIST_CACHE_TTL:
            _, cached_records, feed_exhausted = hit
            if feed_exhausted or len(cached_records) >= max(needed, target):
                logger.info("Playwright cache hit %s (%d records)", key, len(cached_records))
                return cached_records
            logger.info(
                "Playwright cache miss %s (cached %d < needed %d, rescraping)",
                key, len(cached_records), max(needed, target),
            )
        # Cache miss: scrape only what this job needs (one browser launch
        # covers every page via the cache), not a hardcoded 500.
        records, feed_exhausted = self._scrape(query, location, target)
        _LIST_CACHE[key] = (now, records, feed_exhausted)
        return records

    # -- core scrape --------------------------------------------------------
    def _scrape(self, query: str, location: str, target: int) -> tuple[list[dict], bool]:
        search = f"{query} {location}".strip()
        url = f"https://www.google.com/maps/search/{quote_plus(search)}"
        logger.info("Playwright scraping %r (target %d)", search, target)
        records: list[dict] = []
        # True when the feed gave us everything it had (stopped growing on
        # its own) as opposed to stopping because we hit `target`.
        feed_exhausted = True

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage",
                      "--no-sandbox", "--disable-gpu"],
            )
            try:
                context = browser.new_context(
                    locale="en-IN",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1366, "height": 900},
                )
                # Block images/fonts/media: Maps list + detail work without them.
                try:
                    context.route(
                        "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,mp4,webm}",
                        lambda route: route.abort(),
                    )
                except Exception:
                    pass
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                self._pause(0.8)

                # Dismiss consent dialog if present.
                for sel in ("button:has-text('Accept all')", "button:has-text('Accept')"):
                    try:
                        btn = page.query_selector(sel)
                        if btn:
                            btn.click(timeout=2000)
                            self._pause(0.4)
                            break
                    except Exception:
                        pass

                # Wait for either the list feed or a single-place detail pane.
                try:
                    page.wait_for_selector(
                        "div[role='feed'], a.hfpxzc, h1.DUwDvf", timeout=15000
                    )
                except PlaywrightError:
                    logger.warning("No results container found for %r", search)
                    return [], True

                # Single-place result (no feed): enrich directly.
                if not page.query_selector("div[role='feed']") and page.query_selector("h1.DUwDvf"):
                    rec = self._read_detail_pane(page)
                    rec["category"] = rec.get("category") or query
                    return [rec], True

                feed = page.query_selector("div[role='feed']")
                if feed is None:
                    logger.warning("Feed missing for %r", search)
                    return [], True

                # Scroll the feed until it stops growing (full result set)
                # or we hit the safety cap. Cap scales with target so a
                # max_results=5 job does ~12 scrolls, not 120.
                links = page.query_selector_all("a.hfpxzc")
                stable = 0
                stable_needed = _env_int("PLAYWRIGHT_SCROLL_STABLE", 6)
                scroll_cap = min(120, max(12, (target // 3) + 8))
                for _ in range(scroll_cap):  # dynamic cap on scroll iterations
                    if len(links) >= target:
                        feed_exhausted = False  # stopped by target, feed may hold more
                        break
                    prev = len(links)
                    try:
                        page.evaluate(
                            "(el) => el.scrollTo(0, el.scrollHeight)",
                            feed,
                        )
                    except Exception:
                        break
                    time.sleep(self.scroll_delay)
                    links = page.query_selector_all("a.hfpxzc")
                    if len(links) <= prev:
                        stable += 1
                        if stable == 3:
                            # Nudge up then down to retrigger lazy loading;
                            # broad feeds often stall before their true end.
                            try:
                                page.evaluate(
                                    "(el) => el.scrollTo(0, Math.max(0, el.scrollHeight - 1200))",
                                    feed,
                                )
                                time.sleep(self.scroll_delay)
                                page.evaluate(
                                    "(el) => el.scrollTo(0, el.scrollHeight)",
                                    feed,
                                )
                                time.sleep(self.scroll_delay)
                                links = page.query_selector_all("a.hfpxzc")
                                if len(links) > prev:
                                    stable = 0
                                    continue
                            except Exception:
                                pass
                        if stable >= stable_needed:
                            break
                    else:
                        stable = 0
                logger.info("Feed loaded %d cards for %r", len(links), search)

                # Snapshot list-level info first (fast, no navigation).
                cards: list[dict] = []
                for a in links[:target]:
                    try:
                        href = a.get_attribute("href") or None
                        name = (a.get_attribute("aria-label") or "").strip() or None
                        if not name:
                            el = a.query_selector("div.fontHeadlineSmall")
                            name = el.inner_text().strip() if el else None
                        lat, lng = _parse_latlng(href or "")
                        cards.append({"_href": href, "business_name": name,
                                      "google_maps_link": href,
                                      "latitude": lat, "longitude": lng})
                    except Exception:
                        continue

                # Enrich each card by opening its detail pane.
                prev_title: str | None = None
                for i, card in enumerate(cards):
                    try:
                        anchors = page.query_selector_all("a.hfpxzc")
                        if i >= len(anchors):
                            break
                        # Snapshot the currently shown listing so we can tell
                        # whether the pane actually advanced after the click.
                        # (Waiting for h1 *presence* is not enough: the old
                        # listing's h1 satisfies it instantly, yielding the
                        # previous card's data merged into this one.)
                        try:
                            _h1 = page.query_selector("h1.DUwDvf")
                            prev_title = (_h1.inner_text().strip() if _h1 else None) or None
                        except Exception:
                            prev_title = None
                        anchors[i].click(timeout=self.click_timeout)
                        time.sleep(self.detail_pause)
                        try:
                            page.wait_for_function(
                                """(prev) => {
                                    const el = document.querySelector('h1.DUwDvf');
                                    const t = el ? el.innerText.trim() : '';
                                    return t !== '' && t !== prev;
                                }""",
                                arg=prev_title or "",
                                timeout=self.detail_timeout,
                            )
                        except PlaywrightError:
                            # Slow render: one retry before reading the pane.
                            time.sleep(self.detail_pause)
                            try:
                                page.wait_for_function(
                                    """(prev) => {
                                        const el = document.querySelector('h1.DUwDvf');
                                        const t = el ? el.innerText.trim() : '';
                                        return t !== '' && t !== prev;
                                    }""",
                                    arg=prev_title or "",
                                    timeout=self.detail_timeout,
                                )
                            except PlaywrightError:
                                pass
                        detail = self._read_detail_pane(page)
                        if (
                            detail.get("business_name")
                            and prev_title
                            and detail["business_name"] == prev_title
                            and (card.get("business_name") or "") != prev_title
                        ):
                            # Pane never advanced: stale data would duplicate
                            # the previous listing, so keep the snapshot only.
                            logger.debug("Detail pane stuck on %r; using snapshot", prev_title)
                            detail = {}
                        merged = {**card, **{k: v for k, v in detail.items() if v is not None}}
                        if not merged.get("business_name"):
                            merged["business_name"] = card.get("business_name")
                        merged.pop("_href", None)
                        merged["category"] = merged.get("category") or query
                        # area/city derivation from address
                        addr = merged.get("full_address") or ""
                        if addr and not merged.get("area_locality"):
                            merged["area_locality"] = location.split(",")[0].strip()
                        if addr and not merged.get("city_pincode"):
                            merged["city_pincode"] = location
                        if merged.get("business_name"):
                            records.append(merged)
                        # back to list
                        try:
                            back = page.query_selector("button[aria-label='Back']")
                            if back:
                                back.click(timeout=self.click_timeout)
                                time.sleep(0.3)
                        except Exception:
                            pass
                    except Exception as exc:
                        logger.debug("Detail enrich %d failed: %s", i, exc)
                        # Keep the list-level snapshot instead of dropping the card.
                        snapshot = {k: v for k, v in card.items() if v is not None}
                        snapshot.pop("_href", None)
                        snapshot["category"] = snapshot.get("category") or query
                        if snapshot.get("business_name"):
                            records.append(snapshot)
                        # Try to return to the list so the next card starts clean.
                        try:
                            back = page.query_selector("button[aria-label='Back']")
                            if back:
                                back.click(timeout=self.click_timeout)
                                time.sleep(0.3)
                        except Exception:
                            pass

                logger.info("Scraped %d enriched records for %r", len(records), search)
                return records, feed_exhausted
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    # -- detail pane reader -------------------------------------------------
    def _read_detail_pane(self, page) -> dict:
        def _txt(sel: str) -> Optional[str]:
            try:
                el = page.query_selector(sel)
                t = el.inner_text().strip() if el else None
                return t or None
            except Exception:
                return None

        name = _txt("h1.DUwDvf")
        category = _txt("button.DkEaL")
        rating_raw = _txt("div.F7nice span[aria-hidden='true']")
        reviews_raw = _txt("div.F7nice button, div.F7nice span.HHYRVe")
        if reviews_raw is None:
            # Current DOM: reviews live in a button with aria-label "N reviews"
            try:
                el = page.query_selector("div.F7nice button[aria-label*='review' i]")
                reviews_raw = el.get_attribute("aria-label") if el else None
            except Exception:
                reviews_raw = None
        if rating_raw is None or reviews_raw is None:
            # fallback: parse the whole F7nice block, e.g. "4.9(306)"
            try:
                el = page.query_selector("div.F7nice")
                block = el.inner_text() if el else ""
                if rating_raw is None:
                    rating_raw = block.split("\n")[0] if block else None
                if reviews_raw is None and block:
                    m = re.search(r"\(([\d,]+)\)", block.replace("\u00a0", " "))
                    if m:
                        reviews_raw = m.group(1)
            except Exception:
                pass
        address = phone = website = hours = status = None
        try:
            el = page.query_selector("button[data-item-id='address'] div.Io6YTe")
            address = el.inner_text().strip() if el else None
        except Exception:
            pass
        try:
            el = page.query_selector("button[data-item-id^='phone'] div.Io6YTe")
            phone = el.inner_text().strip() if el else None
        except Exception:
            pass
        try:
            el = page.query_selector("a[data-item-id='authority']")
            website = el.get_attribute("href") if el else None
        except Exception:
            pass
        try:
            el = page.query_selector("div[data-item-id='oh']")
            if el:
                hours = " ".join(el.inner_text().split()) or None
        except Exception:
            pass
        try:
            # open-now / temporarily closed indicators
            for sel in ("div.MkV9Kd", "span.MkV9Kd", "div.o0Svhf"):
                el = page.query_selector(sel)
                if el and el.inner_text().strip():
                    status = el.inner_text().strip().split("\n")[0]
                    break
        except Exception:
            pass
        lat, lng = _parse_latlng(page.url or "")
        return {
            "business_name": name,
            "category": category,
            "full_address": address,
            "area_locality": None,
            "city_pincode": None,
            "phone_number": phone,
            "website": website,
            "rating": _parse_rating(rating_raw),
            "total_reviews": _parse_reviews(reviews_raw),
            "opening_hours": hours,
            "business_status": status or "Operational",
            "latitude": lat,
            "longitude": lng,
            "google_maps_link": page.url or None,
            "place_id": None,
        }
