#!/usr/bin/env python3
"""Pull Maui condo market listings, store snapshots, and generate reports."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from maui_market.analysis import (  # noqa: E402
    collect_diff_events,
    diff_snapshots,
    summarize_market,
)
from maui_market.charts import generate_charts  # noqa: E402
from maui_market.config import load_complex_config  # noqa: E402
from maui_market.db import (  # noqa: E402
    connect,
    get_latest_snapshots,
    get_snapshot_listings,
    insert_listing_events,
    snapshot_count,
)
from maui_market.models import SnapshotRecord  # noqa: E402
from maui_market.report import (  # noqa: E402
    build_markdown_report,
    print_stdout_summary,
    write_report,
)
from maui_market.snapshot import dated_export_path, refresh_snapshot  # noqa: E402

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--complex",
        default="maui_kamaole",
        help="Complex config slug (default: maui_kamaole)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Scrape fresh listings and store a new snapshot",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome headless (default is headed browser)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite database path (default: maui_market/history.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape/parse and report without writing DB or export files",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    config = load_complex_config(args.complex)
    conn = connect(args.db)

    try:
        existing_count = snapshot_count(conn, args.complex)
        if existing_count == 0 and not args.refresh and not args.dry_run:
            print("No snapshots yet — run with --refresh to collect listings.")
            return 1

        export_path: Path | None = None
        listings = []
        current_snapshot: SnapshotRecord | None = None
        previous_listings = None
        previous_snapshot_id: int | None = None

        if args.refresh or (existing_count == 0 and not args.dry_run):
            if existing_count == 0 and not args.refresh:
                logger.info("No snapshots found; performing initial refresh")
            snapshot_id, listings, export_path = refresh_snapshot(
                conn,
                args.complex,
                headless=args.headless,
                dry_run=args.dry_run,
            )
            if args.dry_run:
                current_snapshot = SnapshotRecord(
                    id=0,
                    complex_slug=args.complex,
                    scraped_at=datetime.now(),
                    source=config.source,
                    listing_count=len(listings),
                )
            else:
                snapshots = get_latest_snapshots(conn, args.complex, limit=2)
                current_snapshot = snapshots[0]
                if len(snapshots) > 1:
                    previous_snapshot_id = snapshots[1].id
                    previous_listings = get_snapshot_listings(conn, snapshots[1].id)
                events = diff_snapshots(
                    listings,
                    previous_listings,
                    current_snapshot_id=current_snapshot.id,
                    previous_snapshot_id=previous_snapshot_id,
                )
                insert_listing_events(conn, current_snapshot.id, collect_diff_events(events))
        else:
            snapshots = get_latest_snapshots(conn, args.complex, limit=2)
            current_snapshot = snapshots[0]
            listings = get_snapshot_listings(conn, current_snapshot.id)
            export_path = dated_export_path(args.complex, current_snapshot.scraped_at)
            if len(snapshots) > 1:
                previous_snapshot_id = snapshots[1].id
                previous_listings = get_snapshot_listings(conn, snapshots[1].id)

        if not listings or current_snapshot is None:
            print("No listings available to report.")
            return 1

        diff = diff_snapshots(
            listings,
            previous_listings,
            current_snapshot_id=current_snapshot.id,
            previous_snapshot_id=previous_snapshot_id,
        )
        summary = summarize_market(listings)
        chart_paths = (
            [] if args.dry_run else generate_charts(conn, args.complex, listings)
        )
        report_content = build_markdown_report(
            complex_name=config.name,
            snapshot=current_snapshot,
            listings=listings,
            summary=summary,
            diff=diff if previous_listings is not None else None,
            chart_paths=chart_paths,
            export_path=export_path,
        )

        if args.dry_run:
            print(report_content)
            return 0

        report_path = write_report(
            report_content,
            complex_slug=args.complex,
            when=current_snapshot.scraped_at,
        )
        print_stdout_summary(
            complex_name=config.name,
            snapshot=current_snapshot,
            summary=summary,
            diff=diff if previous_listings is not None else None,
            report_path=report_path,
            chart_paths=chart_paths,
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
