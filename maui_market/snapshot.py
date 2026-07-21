from __future__ import annotations

import csv
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from maui_market.config import load_complex_config
from maui_market.db import connect, insert_snapshot
from maui_market.models import ComplexConfig, Listing
from maui_market.scraper.redfin import RedfinScraper

logger = logging.getLogger(__name__)

EXPORTS_DIR = Path(__file__).resolve().parent / "exports"


def scrape_listings(
    config: ComplexConfig,
    *,
    headless: bool = False,
) -> list[Listing]:
    if config.source != "redfin":
        raise ValueError(f"Unsupported source: {config.source}")
    scraper = RedfinScraper(headless=headless)
    return scraper.scrape(config)


def export_snapshot_csv(listings: list[Listing], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "unit",
        "price",
        "bedrooms",
        "bathrooms",
        "sqft",
        "price_per_sqft",
        "status",
        "listing_url",
        "photo_count",
        "photos",
        "description",
        "address",
        "listing_id",
    ]
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for listing in listings:
            writer.writerow(
                {
                    "unit": listing.unit,
                    "price": listing.price if listing.price is not None else "",
                    "bedrooms": listing.bedrooms if listing.bedrooms is not None else "",
                    "bathrooms": listing.bathrooms if listing.bathrooms is not None else "",
                    "sqft": listing.sqft if listing.sqft is not None else "",
                    "price_per_sqft": listing.price_per_sqft
                    if listing.price_per_sqft is not None
                    else "",
                    "status": listing.status,
                    "listing_url": listing.listing_url,
                    "photo_count": len(listing.photo_urls),
                    "photos": "|".join(listing.photo_urls),
                    "description": listing.description,
                    "address": listing.address,
                    "listing_id": listing.listing_id,
                }
            )


def dated_export_path(complex_slug: str, when: datetime | None = None) -> Path:
    stamp = (when or datetime.now()).strftime("%Y_%m_%d")
    return EXPORTS_DIR / f"{complex_slug}_active_{stamp}.csv"


def refresh_snapshot(
    conn: sqlite3.Connection,
    complex_slug: str,
    *,
    headless: bool = False,
    dry_run: bool = False,
) -> tuple[int | None, list[Listing], Path]:
    config = load_complex_config(complex_slug)
    listings = scrape_listings(config, headless=headless)
    scraped_at = datetime.now()
    export_path = dated_export_path(complex_slug, scraped_at)

    if dry_run:
        logger.info("Dry run: would store %d listings", len(listings))
        return None, listings, export_path

    snapshot_id = insert_snapshot(conn, complex_slug, config.source, listings, scraped_at)
    export_snapshot_csv(listings, export_path)
    logger.info("Stored snapshot %d with %d listings", snapshot_id, len(listings))
    return snapshot_id, listings, export_path
