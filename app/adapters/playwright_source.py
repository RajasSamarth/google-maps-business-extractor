"""Playwright-based Google Maps adapter (live data).

Drives a real Chromium browser to https://www.google.com/maps, searches
``query + location``, scrolls the results feed (infinite scroll) and — for
each listing — opens the detail pane to extract phone, website, address,
hours, rating, lat/lng, etc.

Only use after explicit organizational permission to automate Google Maps.
Polite pauses + per-minute rate limiting are built in; no CAPTCHA bypass.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from typing import List, Optional, Tuple
from urllib.parse import quote_plus

from playwright.async_api import async_playwright
from playwright.async_api import Error as PlaywrightError

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

# Safety cap: JOB_WORKERS x PLAYWRIGHT_CONCURRENCY must stay <= 14
# to avoid overloading the device (RAM/CPU + Google 429s).
MAX_PARALLEL_PAGES = 14


def _effective_concurrency(configured: int) -> int:
    """Clamp per-job concurrency so workers x concurrency <= 14."""
    try:
        workers = int(os.getenv("JOB_WORKERS", "3") or "3")
    except (TypeError, ValueError):
        workers = 3
    workers = max(1, workers)
    allowed = max(1, MAX_PARALLEL_PAGES // workers)
    if configured > allowed:
        logger.info(
            "Clamping PLAYWRIGHT_CONCURRENCY %d -> %d (JOB_WORKERS=%d, cap=%d)",
            configured, allowed, workers, MAX_PARALLEL_PAGES,
        )
    return max(1, min(configured, allowed))

_LIST_CACHE: dict[tuple, tuple[float, list[dict], bool]] = {}
_LIST_CACHE_TTL = 600.0  # 10 minutes


def _respect_rate_limit() -> None:
    now = time.time()
    global _request_timestamps
    _request_timestamps = [t for t in _request_timestamps if now - t < 60.0]
    if len(_request_timestamps) >= _MAX_PER_MINUTE:
        wait = 60.0 - (now - min(_request_timestamps)) + 1.0
        time.sleep(max(0.0, wait) * (1 + random.uniform(-0.1, 0.1)))
    _request_timestamps.append(time.time())


def _parse_card_rating(text: Optional[str]) -> tuple[Optional[float], Optional[int]]:
    """Best-effort rating + review count from a result card's text.

    Only trusts a review count when it appears paired with a rating
    (e.g. "4.5 (1,234)"), otherwise a stray number (plot no., floor)
    would become a fake review count.
    """
    if not text:
        return None, None
    clean = text.replace("\u00a0", " ")
    m = re.search(r"(\d[.,]\d)\s*\(([\d,]+)\)", clean)
    if m:
        try:
            return float(m.group(1).replace(",", ".")), int(m.group(2).replace(",", ""))
        except ValueError:
            pass
    # No paired "4.5 (1,234)" pattern: report nothing rather than risk a
    # stray number (plot no., floor) becoming a fake rating.
    return None, None


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
        enrich: bool = True,
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
        # Fast mode skips per-listing detail pages (phone/website/hours) and
        # returns list-level data only: ~50x faster for full-feed runs.
        self.enrich = enrich

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
                _LIST_CACHE.pop((query.lower().strip(), location.lower().strip(), self.enrich), None)
                time.sleep(2 ** (attempt - 1))
        raise last_exc  # type: ignore[misc]

    # -- cache wrapper ------------------------------------------------------
    def _get_or_scrape(self, query: str, location: str, needed: int) -> list[dict]:
        # Fast (list-only) and full (enriched) scrapes are cached separately:
        # their records have different shapes and must never mix.
        key = (query.lower().strip(), location.lower().strip(), self.enrich)
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
        # _scrape is async (parallel detail pages); run it to completion here.
        records, feed_exhausted = asyncio.run(self._scrape(query, location, target))
        _LIST_CACHE[key] = (now, records, feed_exhausted)
        return records

    # -- core scrape --------------------------------------------------------
    async def _scrape(self, query: str, location: str, target: int) -> tuple[list[dict], bool]:
        search = f"{query} {location}".strip()
        url = f"https://www.google.com/maps/search/{quote_plus(search)}"
        logger.info("Playwright scraping %r (target %d)", search, target)
        records: list[dict] = []
        # True when the feed gave us everything it had (stopped growing on
        # its own) as opposed to stopping because we hit `target`.
        feed_exhausted = True
        # Detail pages are enriched N at a time: each card gets
        # its own tab navigated straight to its Maps URL — no click/back
        # choreography, so panes can never go stale.
        # Capped so JOB_WORKERS x CONCURRENCY <= 14.
        concurrency = _effective_concurrency(max(1, _env_int("PLAYWRIGHT_CONCURRENCY", 4)))

        launch_kwargs = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
            ],
        }
        proxy_url = os.getenv("PLAYWRIGHT_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")
        if proxy_url:
            launch_kwargs["proxy"] = {"server": proxy_url}

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(**launch_kwargs)
            try:
                context = await browser.new_context(
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
                    async def _block(route) -> None:
                        await route.abort()

                    await context.route(
                        "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,mp4,webm}",
                        _block,
                    )
                except Exception:
                    pass
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(self.pause_sec * 0.8)

                # Dismiss consent dialog if present.
                for sel in ("button:has-text('Accept all')", "button:has-text('Accept')"):
                    try:
                        btn = await page.query_selector(sel)
                        if btn:
                            await btn.click(timeout=2000)
                            await asyncio.sleep(self.pause_sec * 0.4)
                            break
                    except Exception:
                        pass

                # Wait for either the list feed or a single-place detail pane.
                try:
                    await page.wait_for_selector(
                        "div[role='feed'], a.hfpxzc, h1.DUwDvf", timeout=15000
                    )
                except PlaywrightError:
                    logger.warning("No results container found for %r", search)
                    return [], True

                # Single-place result (no feed): enrich directly.
                if not await page.query_selector("div[role='feed']") and await page.query_selector("h1.DUwDvf"):
                    rec = await self._read_detail_pane(page)
                    rec["category"] = rec.get("category") or query
                    return [rec], True

                feed = await page.query_selector("div[role='feed']")
                if feed is None:
                    logger.warning("Feed missing for %r", search)
                    return [], True

                # Scroll the feed until it stops growing (full result set)
                # or we hit the safety cap. Cap scales with target so a
                # max_results=5 job does ~12 scrolls, not 120.
                links = await page.query_selector_all("a.hfpxzc")
                stable = 0
                stable_needed = _env_int("PLAYWRIGHT_SCROLL_STABLE", 6)
                scroll_cap = min(120, max(12, (target // 3) + 8))
                for _ in range(scroll_cap):  # dynamic cap on scroll iterations
                    if len(links) >= target:
                        feed_exhausted = False  # stopped by target, feed may hold more
                        break
                    prev = len(links)
                    try:
                        await page.evaluate(
                            "(el) => el.scrollTo(0, el.scrollHeight)",
                            feed,
                        )
                    except Exception:
                        break
                    await asyncio.sleep(self.scroll_delay)
                    links = await page.query_selector_all("a.hfpxzc")
                    if len(links) <= prev:
                        stable += 1
                        if stable == 3:
                            # Nudge up then down to retrigger lazy loading;
                            # broad feeds often stall before their true end.
                            try:
                                await page.evaluate(
                                    "(el) => el.scrollTo(0, Math.max(0, el.scrollHeight - 1200))",
                                    feed,
                                )
                                await asyncio.sleep(self.scroll_delay)
                                await page.evaluate(
                                    "(el) => el.scrollTo(0, el.scrollHeight)",
                                    feed,
                                )
                                await asyncio.sleep(self.scroll_delay)
                                links = await page.query_selector_all("a.hfpxzc")
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
                        href = await a.get_attribute("href") or None
                        name = (await a.get_attribute("aria-label") or "").strip() or None
                        if not name:
                            el = await a.query_selector("div.fontHeadlineSmall")
                            name = (await el.inner_text()).strip() if el else None
                        lat, lng = _parse_latlng(href or "")
                        rating, reviews = None, None
                        try:
                            # Nearest ancestor containing this card's own name
                            # plus a rating pair — climbing a fixed height can
                            # overshoot into a container shared by many cards.
                            card_text = await a.evaluate(
                                """(el, nm) => {
                                    if (!nm) return '';
                                    let n = el.parentElement, best = '';
                                    while (n && n !== document.body) {
                                        const t = n.innerText || '';
                                        if (t.includes(nm) && /(\d[.,]\d)\s*\(([\d,]+)\)/.test(t)) { best = t; break; }
                                        n = n.parentElement;
                                    }
                                    return best;
                                }""",
                                name,
                            ) or ""
                            rating, reviews = _parse_card_rating(card_text)
                        except Exception:
                            pass
                        cards.append({"_href": href, "business_name": name,
                                      "google_maps_link": href,
                                      "latitude": lat, "longitude": lng,
                                      "rating": rating, "total_reviews": reviews})
                    except Exception:
                        continue

                if not self.enrich:
                    # Fast mode: return list-level data without opening any
                    # detail pane. No phone/website/hours, but ~50x faster.
                    for card in cards:
                        card.pop("_href", None)
                        card["category"] = card.get("category") or query
                        card["area_locality"] = location.split(",")[0].strip()
                        card["city_pincode"] = location
                        card["business_status"] = "Operational"
                        card["detail_failed"] = True
                        if card.get("business_name"):
                            records.append(card)
                    logger.info("Scraped %d list-level records for %r (fast mode)", len(records), search)
                    return records, feed_exhausted

                # Enrich cards in parallel: each gets its own tab navigated
                # straight to its Maps URL (no click/back choreography, so a
                # pane can never go stale). Concurrency via env, default 4.
                sema = asyncio.Semaphore(concurrency)

                def _snapshot(card: dict, failed: bool = True) -> dict | None:
                    snap = {k: v for k, v in card.items() if v is not None}
                    snap.pop("_href", None)
                    snap["category"] = snap.get("category") or query
                    snap["detail_failed"] = failed
                    return snap if snap.get("business_name") else None

                async def _enrich_one(card: dict, idx: int) -> dict | None:
                    href = card.get("_href") or card.get("google_maps_link")
                    if not href:
                        return _snapshot(card, failed=True)
                    dpage = await context.new_page()
                    try:
                        await dpage.goto(href, wait_until="domcontentloaded", timeout=30000)
                        try:
                            await dpage.wait_for_selector("h1.DUwDvf", timeout=self.detail_timeout)
                        except PlaywrightError:
                            await asyncio.sleep(self.detail_pause)
                        detail = await self._read_detail_pane(dpage)
                        merged = {**card, **{k: v for k, v in detail.items() if v is not None}}
                        if not merged.get("business_name"):
                            merged["business_name"] = card.get("business_name")
                        merged.pop("_href", None)
                        merged["category"] = merged.get("category") or query
                        addr = merged.get("full_address") or ""
                        if addr and not merged.get("area_locality"):
                            merged["area_locality"] = location.split(",")[0].strip()
                        if addr and not merged.get("city_pincode"):
                            merged["city_pincode"] = location
                        merged["detail_failed"] = False
                        return merged if merged.get("business_name") else _snapshot(card, failed=True)
                    except Exception as exc:
                        logger.warning("Detail enrich %d failed, using list data: %s", idx, exc)
                        return _snapshot(card, failed=True)
                    finally:
                        try:
                            await dpage.close()
                        except Exception:
                            pass

                async def _bounded(card: dict, idx: int) -> dict | None:
                    async with sema:
                        if idx:
                            await asyncio.sleep(min(idx * 0.2, 2.0))
                        return await _enrich_one(card, idx)

                enriched = await asyncio.gather(*(_bounded(c, i) for i, c in enumerate(cards)))
                records = [r for r in enriched if r]

                logger.info("Scraped %d enriched records for %r", len(records), search)
                return records, feed_exhausted
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    # -- detail pane reader -------------------------------------------------
    async def _read_detail_pane(self, page) -> dict:
        async def _txt(sel: str) -> Optional[str]:
            try:
                el = await page.query_selector(sel)
                t = (await el.inner_text()).strip() if el else None
                return t or None
            except Exception:
                return None

        async def _attr(sel: str, attr: str) -> Optional[str]:
            try:
                el = await page.query_selector(sel)
                v = await el.get_attribute(attr) if el else None
                return v or None
            except Exception:
                return None

        name = await _txt("h1.DUwDvf")
        category = await _txt("button.DkEaL")
        rating_raw = await _txt("div.F7nice span[aria-hidden='true']")
        reviews_raw = await _txt("div.F7nice button, div.F7nice span.HHYRVe")
        if reviews_raw is None:
            # Current DOM: reviews live in a button with aria-label "N reviews"
            reviews_raw = await _attr("div.F7nice button[aria-label*='review' i]", "aria-label")
        if rating_raw is None or reviews_raw is None:
            # fallback: parse the whole F7nice block, e.g. "4.9(306)"
            try:
                el = await page.query_selector("div.F7nice")
                block = await el.inner_text() if el else ""
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
            el = await page.query_selector("button[data-item-id='address'] div.Io6YTe")
            address = (await el.inner_text()).strip() if el else None
        except Exception:
            pass
        try:
            el = await page.query_selector("button[data-item-id^='phone'] div.Io6YTe")
            phone = (await el.inner_text()).strip() if el else None
        except Exception:
            pass
        try:
            website = await _attr("a[data-item-id='authority']", "href")
        except Exception:
            pass
        try:
            el = await page.query_selector("div[data-item-id='oh']")
            if el:
                hours = " ".join((await el.inner_text()).split()) or None
        except Exception:
            pass
        try:
            # open-now / temporarily closed indicators
            for sel in ("div.MkV9Kd", "span.MkV9Kd", "div.o0Svhf"):
                el = await page.query_selector(sel)
                if el and (await el.inner_text()).strip():
                    status = (await el.inner_text()).strip().split("\n")[0]
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
