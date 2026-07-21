from __future__ import annotations

from datetime import datetime
from pathlib import Path

from maui_market.analysis import estimate_value, summarize_market
from maui_market.models import Listing, MarketSummary, SnapshotDiff, SnapshotRecord

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def _money(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.0f}"


def _format_listing_row(listing: Listing) -> str:
    return (
        f"| {listing.unit or '—'} | {_money(listing.price)} | "
        f"{listing.bedrooms if listing.bedrooms is not None else '—'} | "
        f"{listing.bathrooms if listing.bathrooms is not None else '—'} | "
        f"{listing.sqft if listing.sqft is not None else '—'} | "
        f"{listing.price_per_sqft if listing.price_per_sqft is not None else '—'} | "
        f"{listing.status} | [link]({listing.listing_url}) |"
    )


def _event_lines(events: list, formatter) -> list[str]:
    if not events:
        return ["_None_"]
    return [formatter(event) for event in events]


def build_markdown_report(
    *,
    complex_name: str,
    snapshot: SnapshotRecord,
    listings: list[Listing],
    summary: MarketSummary,
    diff: SnapshotDiff | None,
    chart_paths: list[Path],
    export_path: Path | None,
) -> str:
    lines = [
        f"# {complex_name} market report",
        "",
        f"- Snapshot: {snapshot.scraped_at.isoformat(timespec='seconds')}",
        f"- Source: {snapshot.source}",
        f"- Active listings captured: {snapshot.listing_count}",
    ]
    if export_path is not None:
        lines.append(f"- Export CSV: `{export_path}`")
    lines.extend(
        [
            "",
            "## Market summary",
            "",
            f"- Median asking price: {_money(summary.median_price)}",
            f"- Mean asking price: {_money(summary.mean_price)}",
            f"- Median $/sqft: {_money(summary.median_price_per_sqft)} "
            f"({summary.ppsf_sample_count} listings with sqft)",
            f"- Inventory: {summary.listing_count}",
            "",
            "### By bedrooms",
            "",
        ]
    )
    for bed, count in summary.by_bedrooms.items():
        lines.append(f"- {bed}: {count}")
    lines.extend(["", "### By status", ""])
    for status, count in summary.by_status.items():
        lines.append(f"- {status}: {count}")

    if diff is not None:
        lines.extend(["", "## Changes since prior snapshot", ""])
        lines.extend(
            [
                "### New listings",
                "",
                *_event_lines(
                    diff.new_listings,
                    lambda listing: (
                        f"- {listing.unit or listing.address}: {_money(listing.price)} "
                        f"— [link]({listing.listing_url})"
                    ),
                ),
                "",
                "### Price reductions",
                "",
                *_event_lines(
                    diff.price_reductions,
                    lambda event: (
                        f"- {event.unit or event.address}: "
                        f"{_money(event.old_price)} → {_money(event.new_price)} "
                        f"— [link]({event.listing_url})"
                    ),
                ),
                "",
                "### Pending / contingent",
                "",
                *_event_lines(
                    diff.status_pending,
                    lambda event: (
                        f"- {event.unit or event.address}: {event.old_status} → {event.new_status} "
                        f"— [link]({event.listing_url})"
                    ),
                ),
                "",
                "### Removed listings",
                "",
                *_event_lines(
                    diff.removed_listings,
                    lambda listing: (
                        f"- {listing.unit or listing.address}: {_money(listing.price)} "
                        f"— last seen at [link]({listing.listing_url})"
                    ),
                ),
            ]
        )

    lines.extend(
        [
            "",
            "## Active listings",
            "",
            "| Unit | Price | Beds | Baths | Sqft | $/Sqft | Status | URL |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for listing in sorted(listings, key=lambda item: (item.unit, item.address)):
        lines.append(_format_listing_row(listing))

    if chart_paths:
        lines.extend(["", "## Charts", ""])
        for chart_path in chart_paths:
            lines.append(f"![{chart_path.name}]({chart_path})")

    sample_valuation = estimate_value(listings, bedrooms=2, sqft=850)
    lines.extend(
        [
            "",
            "## Valuation helper (example)",
            "",
            f"- Example 2BR / 850 sqft estimate: {_money(sample_valuation['estimated_price'])}",
            f"- Median comp $/sqft: {sample_valuation['median_ppsf']}",
            f"- Note: {sample_valuation['note']}",
        ]
    )
    return "\n".join(lines) + "\n"


def print_stdout_summary(
    *,
    complex_name: str,
    snapshot: SnapshotRecord,
    summary: MarketSummary,
    diff: SnapshotDiff | None,
    report_path: Path,
    chart_paths: list[Path],
) -> None:
    print()
    print(f"{complex_name} market summary")
    print(f"Snapshot: {snapshot.scraped_at.strftime('%Y-%m-%d %H:%M')}")
    print(f"Inventory: {summary.listing_count} listings")
    print(f"Median asking price: {_money(summary.median_price)}")
    print(
        f"Median $/sqft: {_money(summary.median_price_per_sqft)} "
        f"({summary.ppsf_sample_count} with sqft)"
    )
    if diff is not None:
        print(
            "Changes since prior snapshot: "
            f"{len(diff.new_listings)} new, "
            f"{len(diff.price_reductions)} price reductions, "
            f"{len(diff.status_pending)} pending, "
            f"{len(diff.removed_listings)} removed"
        )
    else:
        print("Changes since prior snapshot: first snapshot (no prior data)")
    print(f"Report: {report_path}")
    if chart_paths:
        print("Charts:")
        for chart_path in chart_paths:
            print(f"  - {chart_path}")
    print()


def write_report(
    content: str,
    *,
    complex_slug: str,
    when: datetime | None = None,
) -> Path:
    stamp = (when or datetime.now()).strftime("%Y_%m_%d")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{complex_slug}_report_{stamp}.md"
    path.write_text(content, encoding="utf-8")
    return path
