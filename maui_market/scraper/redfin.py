from __future__ import annotations

import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from maui_market.models import ComplexConfig, Listing
from maui_market.units import (
    apply_listing_identity,
    listing_matches_complex,
    load_unit_registry,
    parse_unit,
    parse_unit_from_url,
)

logger = logging.getLogger(__name__)

DEBUG_DIR = Path(__file__).resolve().parent / "debug"

PENDING_STATUSES = frozenset({"pending", "contingent", "under contract", "active under contract"})
ACTIVE_STATUSES = frozenset({"active", "for sale", "new"})
MIN_LISTING_PRICE = 100_000


def parse_price(value: str | int | float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits) if digits else None


def parse_float(value: str | int | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"(\d+(?:\.\d+)?)", str(value))
    return float(match.group(1)) if match else None


def parse_int(value: str | int | float | None) -> int | None:
    parsed = parse_float(value)
    return int(parsed) if parsed is not None else None


def normalize_status(value: str | None) -> str:
    if not value:
        return "active"
    lowered = value.strip().lower()
    if any(token in lowered for token in ("pending", "contingent", "under contract")):
        return "pending"
    return "active"


def extract_listing_id_from_url(url: str) -> str:
    match = re.search(r"/home/(\d+)", url)
    if match:
        return match.group(1)
    match = re.search(r"(\d{6,})", url)
    return match.group(1) if match else url.rstrip("/").split("/")[-1]


def _walk_json(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_json(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_json(item)


def _candidate_home_dicts(payload: Any) -> list[dict[str, Any]]:
    homes: list[dict[str, Any]] = []
    for node in _walk_json(payload):
        if not isinstance(node, dict):
            continue
        url = node.get("url") or node.get("propertyUrl") or node.get("listingUrl")
        price = node.get("price") or node.get("listPrice") or node.get("amount")
        if url and price is not None and "/home/" in str(url):
            homes.append(node)
    deduped: dict[str, dict[str, Any]] = {}
    for home in homes:
        listing_id = extract_listing_id_from_url(str(home.get("url") or home.get("propertyUrl")))
        deduped[listing_id] = home
    return list(deduped.values())


def listing_from_home_dict(home: dict[str, Any], config: ComplexConfig) -> Listing | None:
    url = str(home.get("url") or home.get("propertyUrl") or home.get("listingUrl") or "")
    if not url:
        return None
    if url.startswith("/"):
        url = urljoin("https://www.redfin.com", url)
    listing_id = extract_listing_id_from_url(url)
    address = str(
        home.get("streetLine")
        or home.get("formattedStreetLine")
        or home.get("address")
        or home.get("location")
        or ""
    )
    if isinstance(home.get("address"), dict):
        addr = home["address"]
        address = str(addr.get("streetAddress") or addr.get("formattedStreetLine") or address)
    unit = parse_unit_from_url(url) or parse_unit(address, config.address_pattern)
    price = parse_price(home.get("price") or home.get("listPrice") or home.get("amount"))
    sqft = parse_int(home.get("sqFt") or home.get("squareFeet") or home.get("lotSize"))
    bedrooms = parse_float(home.get("beds") or home.get("bedrooms"))
    bathrooms = parse_float(home.get("baths") or home.get("bathrooms"))
    status = normalize_status(str(home.get("status") or home.get("mlsStatus") or "active"))
    listing = Listing(
        listing_id=listing_id,
        unit=unit,
        address=address,
        price=price,
        bedrooms=bedrooms,
        bathrooms=bathrooms,
        sqft=sqft,
        price_per_sqft=None,
        status=status,
        listing_url=url,
    )
    listing.price_per_sqft = listing.compute_price_per_sqft()
    return listing


def parse_next_data(html: str, config: ComplexConfig) -> list[Listing]:
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        return []
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    listings: list[Listing] = []
    for home in _candidate_home_dicts(payload):
        listing = listing_from_home_dict(home, config)
        if listing is not None:
            listings.append(listing)
    return listings


def parse_dom_cards(html: str, config: ComplexConfig) -> list[Listing]:
    listings: list[Listing] = []
    card_pattern = re.compile(
        r'href="(/[^"]+/home/\d+)"[^>]*>.*?'
        r'(?:\$[\d,]+).*?'
        r'(?:bed|bd).*?'
        r'(?:bath|ba)',
        re.DOTALL | re.IGNORECASE,
    )
    urls = sorted(set(re.findall(r'href="(/[^"]+/home/\d+)"', html)))
    for rel_url in urls:
        url = urljoin("https://www.redfin.com", rel_url)
        listing_id = extract_listing_id_from_url(url)
        window_start = html.find(rel_url)
        window = html[window_start : window_start + 2500] if window_start >= 0 else ""
        price = parse_price(re.search(r"\$[\d,]+", window).group(0) if re.search(r"\$[\d,]+", window) else None)
        beds = parse_float(
            re.search(r"(\d+(?:\.\d+)?)\s*(?:bd|bed)", window, re.I).group(1)
            if re.search(r"(\d+(?:\.\d+)?)\s*(?:bd|bed)", window, re.I)
            else None
        )
        baths = parse_float(
            re.search(r"(\d+(?:\.\d+)?)\s*(?:ba|bath)", window, re.I).group(1)
            if re.search(r"(\d+(?:\.\d+)?)\s*(?:ba|bath)", window, re.I)
            else None
        )
        sqft = parse_int(
            re.search(r"([\d,]+)\s*(?:sq\s*ft|sqft)", window, re.I).group(1)
            if re.search(r"([\d,]+)\s*(?:sq\s*ft|sqft)", window, re.I)
            else None
        )
        address_match = re.search(
            r'address[^>]*>([^<]+)</',
            window,
            re.I,
        )
        address = address_match.group(1).strip() if address_match else ""
        listing = Listing(
            listing_id=listing_id,
            unit=parse_unit_from_url(url) or parse_unit(address, config.address_pattern),
            address=address,
            price=price,
            bedrooms=beds,
            bathrooms=baths,
            sqft=sqft,
            price_per_sqft=None,
            status="active",
            listing_url=url,
        )
        listing.price_per_sqft = listing.compute_price_per_sqft()
        listings.append(listing)
    _ = card_pattern  # reserved for future selector tuning
    return listings


def parse_detail_page(html: str) -> tuple[str, list[str], str]:
    description = ""
    desc_match = re.search(
        r'(?:marketingRemarks|description)["\']?\s*:\s*"([^"]+)"',
        html,
    )
    if desc_match:
        description = desc_match.group(1).encode("utf-8").decode("unicode_escape")
    if not description:
        for pattern in (
            r'<div[^>]*class="[^"]*remarks[^"]*"[^>]*>(.*?)</div>',
            r'<section[^>]*id="about-this-home"[^>]*>(.*?)</section>',
        ):
            block = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if block:
                text = re.sub(r"<[^>]+>", " ", block.group(1))
                description = re.sub(r"\s+", " ", text).strip()
                if description:
                    break

    photo_urls = sorted(
        set(
            url.replace("\\u002F", "/")
            for url in re.findall(r'https://[^"\']+(?:\.jpg|\.jpeg|\.png|\.webp)[^"\']*', html, re.I)
            if "redfin" in url.lower() or "ssl.cdn-redfin" in url.lower()
        )
    )

    status = "active"
    status_match = re.search(r'"mlsStatus"\s*:\s*"([^"]+)"', html)
    if status_match:
        status = normalize_status(status_match.group(1))
    return description, photo_urls[:30], status


def parse_listing_price(html: str, existing: int | None = None) -> int | None:
    if existing is not None and existing >= MIN_LISTING_PRICE:
        return existing
    for pattern in (
        r'"listPrice"\s*:\s*(\d+)',
        r'"avmPrice"\s*:\s*(\d+)',
        r'"price"\s*:\s*(\d+)',
    ):
        for match in re.finditer(pattern, html):
            price = int(match.group(1))
            if price >= MIN_LISTING_PRICE:
                return price
    return existing if existing is not None and existing >= MIN_LISTING_PRICE else None


def enrich_listing_from_detail(listing: Listing, html: str, config: ComplexConfig) -> Listing:
    description, photo_urls, status = parse_detail_page(html)
    street_match = re.search(r'"streetAddress"\s*:\s*"([^"]+)"', html)
    if street_match:
        listing.address = street_match.group(1)
    if description:
        listing.description = description
    if photo_urls:
        listing.photo_urls = photo_urls
    listing.status = status
    if listing.sqft is None:
        sqft_match = re.search(r'"sqFt"\s*:\s*(\d+)', html)
        if sqft_match:
            listing.sqft = int(sqft_match.group(1))
    if listing.bedrooms is None:
        beds_match = re.search(r'"beds"\s*:\s*(\d+(?:\.\d+)?)', html)
        if beds_match:
            listing.bedrooms = float(beds_match.group(1))
    if listing.bathrooms is None:
        baths_match = re.search(r'"baths"\s*:\s*(\d+(?:\.\d+)?)', html)
        if baths_match:
            listing.bathrooms = float(baths_match.group(1))
    listing.price = parse_listing_price(html, listing.price)
    listing.price_per_sqft = listing.compute_price_per_sqft()
    return listing


class RedfinScraper:
    def __init__(
        self,
        *,
        headless: bool = False,
        request_delay: tuple[float, float] = (2.0, 4.0),
        max_retries: int = 2,
    ) -> None:
        self.headless = headless
        self.request_delay = request_delay
        self.max_retries = max_retries

    def scrape(self, config: ComplexConfig) -> list[Listing]:
        driver = self._create_driver()
        registry = load_unit_registry(config)
        try:
            search_html = self._fetch_with_retries(driver, config.search_url)
            listings = parse_next_data(search_html, config)
            if not listings:
                listings = parse_dom_cards(search_html, config)
            if not listings:
                self._save_debug("search_empty.html", search_html)
                raise RuntimeError(
                    f"No listings parsed from Redfin search page: {config.search_url}"
                )
            raw_count = len(listings)
            listings = [listing for listing in listings if listing_matches_complex(listing, config)]
            if not listings:
                self._save_debug("search_filtered_empty.html", search_html)
                raise RuntimeError(
                    f"No {config.street_number} S Kihei Rd listings after address filter"
                )
            logger.info(
                "Found %d listings on search page (%d after %s filter)",
                raw_count,
                len(listings),
                config.street_number,
            )
            enriched: list[Listing] = []
            for index, listing in enumerate(listings, start=1):
                logger.info(
                    "Fetching detail %d/%d: %s",
                    index,
                    len(listings),
                    listing.listing_url,
                )
                detail_html = self._fetch_with_retries(driver, listing.listing_url)
                listing = enrich_listing_from_detail(listing, detail_html, config)
                listing = apply_listing_identity(listing, config, registry)
                if listing is not None:
                    enriched.append(listing)
                self._sleep()
            return enriched
        finally:
            driver.quit()

    def _create_driver(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--window-size=1400,1000")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(60)
        return driver

    def _fetch_with_retries(self, driver, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 2):
            try:
                driver.get(url)
                self._sleep(short=True)
                self._scroll_page(driver)
                return driver.page_source
            except Exception as exc:  # noqa: BLE001 - retry wrapper
                last_error = exc
                logger.warning("Attempt %d failed for %s: %s", attempt, url, exc)
                self._sleep(short=True)
        raise RuntimeError(f"Failed to load {url}") from last_error

    def _scroll_page(self, driver) -> None:
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.0)
            driver.execute_script("window.scrollTo(0, 0);")
        except Exception:  # noqa: BLE001
            pass

    def _sleep(self, *, short: bool = False) -> None:
        low, high = self.request_delay
        if short:
            time.sleep(random.uniform(low / 2, high / 2))
        else:
            time.sleep(random.uniform(low, high))

    def _save_debug(self, filename: str, html: str) -> None:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path = DEBUG_DIR / filename
        path.write_text(html, encoding="utf-8")
        logger.warning("Saved debug HTML to %s", path)
