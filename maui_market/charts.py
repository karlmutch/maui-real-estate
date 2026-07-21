from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt

from maui_market.analysis import summarize_market
from maui_market.db import get_all_snapshots, get_snapshot_listings
from maui_market.models import Listing, SnapshotRecord

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def _snapshot_metrics(conn: sqlite3.Connection, snapshot: SnapshotRecord) -> dict[str, float | int | None]:
    listings = get_snapshot_listings(conn, snapshot.id)
    summary = summarize_market(listings)
    return {
        "date": snapshot.scraped_at.date().isoformat(),
        "listing_count": summary.listing_count,
        "median_price": summary.median_price,
        "median_ppsf": summary.median_price_per_sqft,
    }


def generate_charts(
    conn: sqlite3.Connection,
    complex_slug: str,
    current_listings: list[Listing],
    *,
    output_dir: Path | None = None,
) -> list[Path]:
    snapshots = get_all_snapshots(conn, complex_slug)
    stamp = snapshots[-1].scraped_at.strftime("%Y_%m_%d") if snapshots else "latest"
    chart_dir = output_dir or (REPORTS_DIR / f"charts_{stamp}")
    chart_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if len(snapshots) >= 2:
        metrics = [_snapshot_metrics(conn, snapshot) for snapshot in snapshots]
        dates = [item["date"] for item in metrics]
        counts = [int(item["listing_count"] or 0) for item in metrics]
        median_prices = [item["median_price"] for item in metrics if item["median_price"] is not None]
        median_dates = [
            item["date"] for item in metrics if item["median_price"] is not None
        ]
        median_ppsf = [item["median_ppsf"] for item in metrics if item["median_ppsf"] is not None]
        ppsf_dates = [item["date"] for item in metrics if item["median_ppsf"] is not None]
        x_positions = list(range(len(dates)))

        inventory_path = chart_dir / "inventory_trend.png"
        plt.figure(figsize=(8, 4))
        plt.plot(x_positions, counts, marker="o")
        plt.title("Active listing inventory")
        plt.xlabel("Snapshot date")
        plt.ylabel("Listings")
        plt.xticks(x_positions, dates, rotation=30, ha="right")
        plt.tight_layout()
        plt.savefig(inventory_path)
        plt.close()
        written.append(inventory_path)

        if median_prices:
            price_path = chart_dir / "median_price_trend.png"
            price_x = list(range(len(median_dates)))
            plt.figure(figsize=(8, 4))
            plt.plot(price_x, median_prices, marker="o", color="tab:green")
            plt.title("Median asking price")
            plt.xlabel("Snapshot date")
            plt.ylabel("USD")
            plt.xticks(price_x, median_dates, rotation=30, ha="right")
            plt.tight_layout()
            plt.savefig(price_path)
            plt.close()
            written.append(price_path)

        if median_ppsf:
            ppsf_path = chart_dir / "median_ppsf_trend.png"
            ppsf_x = list(range(len(ppsf_dates)))
            plt.figure(figsize=(8, 4))
            plt.plot(ppsf_x, median_ppsf, marker="o", color="tab:purple")
            plt.title("Median asking price per sqft")
            plt.xlabel("Snapshot date")
            plt.ylabel("USD / sqft")
            plt.xticks(ppsf_x, ppsf_dates, rotation=30, ha="right")
            plt.tight_layout()
            plt.savefig(ppsf_path)
            plt.close()
            written.append(ppsf_path)
    else:
        logger.info("Trend charts require at least two snapshots")

    active = [listing for listing in current_listings if listing.status == "active"]
    by_bed: dict[str, list[int]] = {}
    for listing in active:
        if listing.price is None:
            continue
        key = (
            str(int(listing.bedrooms))
            if listing.bedrooms is not None and listing.bedrooms == int(listing.bedrooms)
            else (str(listing.bedrooms) if listing.bedrooms is not None else "unknown")
        )
        by_bed.setdefault(key, []).append(listing.price)
    if by_bed:
        bed_path = chart_dir / "price_by_bedrooms.png"
        labels = sorted(by_bed.keys())
        values = [by_bed[label] for label in labels]
        plt.figure(figsize=(8, 4))
        plt.boxplot(values, tick_labels=labels)
        plt.title("Current asking price by bedrooms")
        plt.xlabel("Bedrooms")
        plt.ylabel("USD")
        plt.tight_layout()
        plt.savefig(bed_path)
        plt.close()
        written.append(bed_path)

    return written
