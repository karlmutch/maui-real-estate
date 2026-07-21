from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from maui_market.models import Listing, ListingEvent, SnapshotRecord

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "history.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complex_slug TEXT NOT NULL,
    scraped_at TEXT NOT NULL,
    source TEXT NOT NULL,
    listing_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    listing_id TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    price INTEGER,
    bedrooms REAL,
    bathrooms REAL,
    sqft INTEGER,
    price_per_sqft REAL,
    status TEXT NOT NULL DEFAULT 'active',
    listing_url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    photo_urls TEXT NOT NULL DEFAULT '[]',
    UNIQUE(snapshot_id, listing_id)
);

CREATE TABLE IF NOT EXISTS listing_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    event_type TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    listing_url TEXT NOT NULL DEFAULT '',
    old_price INTEGER,
    new_price INTEGER,
    old_status TEXT,
    new_status TEXT
);

CREATE INDEX IF NOT EXISTS idx_snapshots_complex ON snapshots(complex_slug, scraped_at);
CREATE INDEX IF NOT EXISTS idx_listings_snapshot ON listings(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_listings_listing_id ON listings(listing_id);
CREATE INDEX IF NOT EXISTS idx_events_snapshot ON listing_events(snapshot_id);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def snapshot_count(conn: sqlite3.Connection, complex_slug: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM snapshots WHERE complex_slug = ?",
        (complex_slug,),
    ).fetchone()
    return int(row["c"])


def insert_snapshot(
    conn: sqlite3.Connection,
    complex_slug: str,
    source: str,
    listings: list[Listing],
    scraped_at: datetime | None = None,
) -> int:
    when = scraped_at or datetime.now()
    cursor = conn.execute(
        """
        INSERT INTO snapshots (complex_slug, scraped_at, source, listing_count)
        VALUES (?, ?, ?, ?)
        """,
        (complex_slug, when.isoformat(timespec="seconds"), source, len(listings)),
    )
    snapshot_id = int(cursor.lastrowid)
    for listing in listings:
        ppsf = listing.price_per_sqft
        if ppsf is None:
            ppsf = listing.compute_price_per_sqft()
        conn.execute(
            """
            INSERT INTO listings (
                snapshot_id, listing_id, unit, address, price, bedrooms, bathrooms,
                sqft, price_per_sqft, status, listing_url, description, photo_urls
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                listing.listing_id,
                listing.unit,
                listing.address,
                listing.price,
                listing.bedrooms,
                listing.bathrooms,
                listing.sqft,
                ppsf,
                listing.status,
                listing.listing_url,
                listing.description,
                json.dumps(listing.photo_urls),
            ),
        )
    conn.commit()
    return snapshot_id


def insert_listing_events(
    conn: sqlite3.Connection,
    snapshot_id: int,
    events: list[ListingEvent],
) -> None:
    conn.execute("DELETE FROM listing_events WHERE snapshot_id = ?", (snapshot_id,))
    for event in events:
        conn.execute(
            """
            INSERT INTO listing_events (
                snapshot_id, event_type, listing_id, unit, address, listing_url,
                old_price, new_price, old_status, new_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                event.event_type,
                event.listing_id,
                event.unit,
                event.address,
                event.listing_url,
                event.old_price,
                event.new_price,
                event.old_status,
                event.new_status,
            ),
        )
    conn.commit()


def get_latest_snapshots(
    conn: sqlite3.Connection,
    complex_slug: str,
    limit: int = 2,
) -> list[SnapshotRecord]:
    rows = conn.execute(
        """
        SELECT id, complex_slug, scraped_at, source, listing_count
        FROM snapshots
        WHERE complex_slug = ?
        ORDER BY scraped_at DESC, id DESC
        LIMIT ?
        """,
        (complex_slug, limit),
    ).fetchall()
    return [
        SnapshotRecord(
            id=int(row["id"]),
            complex_slug=row["complex_slug"],
            scraped_at=datetime.fromisoformat(row["scraped_at"]),
            source=row["source"],
            listing_count=int(row["listing_count"]),
        )
        for row in rows
    ]


def get_all_snapshots(conn: sqlite3.Connection, complex_slug: str) -> list[SnapshotRecord]:
    rows = conn.execute(
        """
        SELECT id, complex_slug, scraped_at, source, listing_count
        FROM snapshots
        WHERE complex_slug = ?
        ORDER BY scraped_at ASC, id ASC
        """,
        (complex_slug,),
    ).fetchall()
    return [
        SnapshotRecord(
            id=int(row["id"]),
            complex_slug=row["complex_slug"],
            scraped_at=datetime.fromisoformat(row["scraped_at"]),
            source=row["source"],
            listing_count=int(row["listing_count"]),
        )
        for row in rows
    ]


def _row_to_listing(row: sqlite3.Row) -> Listing:
    return Listing(
        listing_id=row["listing_id"],
        unit=row["unit"] or "",
        address=row["address"] or "",
        price=row["price"],
        bedrooms=row["bedrooms"],
        bathrooms=row["bathrooms"],
        sqft=row["sqft"],
        price_per_sqft=row["price_per_sqft"],
        status=row["status"] or "active",
        listing_url=row["listing_url"] or "",
        description=row["description"] or "",
        photo_urls=json.loads(row["photo_urls"] or "[]"),
    )


def get_snapshot_listings(conn: sqlite3.Connection, snapshot_id: int) -> list[Listing]:
    rows = conn.execute(
        """
        SELECT listing_id, unit, address, price, bedrooms, bathrooms, sqft,
               price_per_sqft, status, listing_url, description, photo_urls
        FROM listings
        WHERE snapshot_id = ?
        ORDER BY unit, address
        """,
        (snapshot_id,),
    ).fetchall()
    return [_row_to_listing(row) for row in rows]
