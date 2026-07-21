from __future__ import annotations

import statistics
from typing import Iterable

from maui_market.models import Listing, ListingEvent, MarketSummary, SnapshotDiff

PENDING_STATUSES = frozenset({"pending", "contingent"})


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def summarize_market(listings: list[Listing]) -> MarketSummary:
    prices = [float(listing.price) for listing in listings if listing.price is not None]
    ppsf_values = [
        float(listing.price_per_sqft)
        for listing in listings
        if listing.price_per_sqft is not None
    ]
    by_bedrooms: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for listing in listings:
        bed_key = (
            str(int(listing.bedrooms))
            if listing.bedrooms is not None and listing.bedrooms == int(listing.bedrooms)
            else (str(listing.bedrooms) if listing.bedrooms is not None else "unknown")
        )
        by_bedrooms[bed_key] = by_bedrooms.get(bed_key, 0) + 1
        status_key = listing.status or "active"
        by_status[status_key] = by_status.get(status_key, 0) + 1
    return MarketSummary(
        listing_count=len(listings),
        median_price=_median(prices),
        mean_price=_mean(prices),
        median_price_per_sqft=_median(ppsf_values),
        mean_price_per_sqft=_mean(ppsf_values),
        ppsf_sample_count=len(ppsf_values),
        by_bedrooms=dict(sorted(by_bedrooms.items())),
        by_status=dict(sorted(by_status.items())),
    )


def estimate_value(
    listings: list[Listing],
    *,
    unit: str | None = None,
    bedrooms: float | None = None,
    sqft: int | None = None,
) -> dict[str, float | int | str | None]:
    """Estimate asking-price range from current active comps."""
    active = [listing for listing in listings if listing.status == "active"]
    if unit:
        matches = [listing for listing in active if listing.unit.upper() == unit.upper()]
        if matches and matches[0].sqft:
            sqft = matches[0].sqft
        if matches and matches[0].bedrooms is not None:
            bedrooms = matches[0].bedrooms

    bed_filtered = active
    if bedrooms is not None:
        bed_filtered = [
            listing
            for listing in active
            if listing.bedrooms is not None and abs(listing.bedrooms - bedrooms) < 0.01
        ]
    ppsf_pool = [
        float(listing.price_per_sqft)
        for listing in (bed_filtered or active)
        if listing.price_per_sqft is not None
    ]
    if not ppsf_pool:
        ppsf_pool = [
            float(listing.price_per_sqft)
            for listing in active
            if listing.price_per_sqft is not None
        ]
    median_ppsf = _median(ppsf_pool)
    if median_ppsf is None:
        return {
            "unit": unit,
            "bedrooms": bedrooms,
            "sqft": sqft,
            "estimated_price": None,
            "median_ppsf": None,
            "comp_count": 0,
            "note": "Insufficient comp data for valuation.",
        }
    estimated = int(round(median_ppsf * sqft)) if sqft else None
    return {
        "unit": unit,
        "bedrooms": bedrooms,
        "sqft": sqft,
        "estimated_price": estimated,
        "median_ppsf": round(median_ppsf, 2),
        "comp_count": len(ppsf_pool),
        "note": "Asking-price comps only; not appraised or county assessed value.",
    }


def _listing_map(listings: Iterable[Listing]) -> dict[str, Listing]:
    return {listing.listing_id: listing for listing in listings}


def diff_snapshots(
    current: list[Listing],
    previous: list[Listing] | None,
    *,
    current_snapshot_id: int,
    previous_snapshot_id: int | None,
) -> SnapshotDiff:
    diff = SnapshotDiff(
        current_snapshot_id=current_snapshot_id,
        previous_snapshot_id=previous_snapshot_id,
    )
    if previous is None:
        diff.new_listings = list(current)
        return diff

    current_map = _listing_map(current)
    previous_map = _listing_map(previous)
    current_ids = set(current_map)
    previous_ids = set(previous_map)

    diff.new_listings = [current_map[listing_id] for listing_id in sorted(current_ids - previous_ids)]
    diff.removed_listings = [
        previous_map[listing_id] for listing_id in sorted(previous_ids - current_ids)
    ]

    for listing_id in sorted(current_ids & previous_ids):
        cur = current_map[listing_id]
        prev = previous_map[listing_id]
        if cur.price is not None and prev.price is not None:
            if cur.price < prev.price:
                diff.price_reductions.append(
                    ListingEvent(
                        event_type="price_reduction",
                        listing_id=listing_id,
                        unit=cur.unit,
                        address=cur.address,
                        listing_url=cur.listing_url,
                        old_price=prev.price,
                        new_price=cur.price,
                    )
                )
            elif cur.price > prev.price:
                diff.price_increases.append(
                    ListingEvent(
                        event_type="price_increase",
                        listing_id=listing_id,
                        unit=cur.unit,
                        address=cur.address,
                        listing_url=cur.listing_url,
                        old_price=prev.price,
                        new_price=cur.price,
                    )
                )
        prev_pending = (prev.status or "").lower() in PENDING_STATUSES
        cur_pending = (cur.status or "").lower() in PENDING_STATUSES
        if not prev_pending and cur_pending:
            diff.status_pending.append(
                ListingEvent(
                    event_type="status_pending",
                    listing_id=listing_id,
                    unit=cur.unit,
                    address=cur.address,
                    listing_url=cur.listing_url,
                    old_status=prev.status,
                    new_status=cur.status,
                )
            )
        elif prev_pending and not cur_pending:
            diff.status_active.append(
                ListingEvent(
                    event_type="status_active",
                    listing_id=listing_id,
                    unit=cur.unit,
                    address=cur.address,
                    listing_url=cur.listing_url,
                    old_status=prev.status,
                    new_status=cur.status,
                )
            )
    return diff


def collect_diff_events(diff: SnapshotDiff) -> list[ListingEvent]:
    return [
        *[
            ListingEvent(
                event_type="new",
                listing_id=listing.listing_id,
                unit=listing.unit,
                address=listing.address,
                listing_url=listing.listing_url,
                new_price=listing.price,
                new_status=listing.status,
            )
            for listing in diff.new_listings
        ],
        *[
            ListingEvent(
                event_type="removed",
                listing_id=listing.listing_id,
                unit=listing.unit,
                address=listing.address,
                listing_url=listing.listing_url,
                old_price=listing.price,
                old_status=listing.status,
            )
            for listing in diff.removed_listings
        ],
        *diff.price_reductions,
        *diff.price_increases,
        *diff.status_pending,
        *diff.status_active,
    ]
