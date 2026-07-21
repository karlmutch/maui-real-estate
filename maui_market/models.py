from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class ComplexConfig:
    slug: str
    name: str
    source: str
    search_url: str
    address_pattern: str
    street_number: str = ""
    street_name: str = "Kihei"
    tmks_file: str = ""
    data_root: str = "data"
    output_prefix: str = ""


@dataclass
class Listing:
    listing_id: str
    unit: str
    address: str
    price: int | None
    bedrooms: float | None
    bathrooms: float | None
    sqft: int | None
    price_per_sqft: float | None
    status: str
    listing_url: str
    description: str = ""
    photo_urls: list[str] = field(default_factory=list)

    def compute_price_per_sqft(self) -> float | None:
        if self.price is None or not self.sqft:
            return None
        return round(self.price / self.sqft, 2)


@dataclass(frozen=True)
class SnapshotRecord:
    id: int
    complex_slug: str
    scraped_at: datetime
    source: str
    listing_count: int


@dataclass
class ListingEvent:
    event_type: str
    listing_id: str
    unit: str
    address: str
    listing_url: str
    old_price: int | None = None
    new_price: int | None = None
    old_status: str | None = None
    new_status: str | None = None


@dataclass
class SnapshotDiff:
    current_snapshot_id: int
    previous_snapshot_id: int | None
    new_listings: list[Listing] = field(default_factory=list)
    removed_listings: list[Listing] = field(default_factory=list)
    price_reductions: list[ListingEvent] = field(default_factory=list)
    price_increases: list[ListingEvent] = field(default_factory=list)
    status_pending: list[ListingEvent] = field(default_factory=list)
    status_active: list[ListingEvent] = field(default_factory=list)


@dataclass
class MarketSummary:
    listing_count: int
    median_price: float | None
    mean_price: float | None
    median_price_per_sqft: float | None
    mean_price_per_sqft: float | None
    ppsf_sample_count: int
    by_bedrooms: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
