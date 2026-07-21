#!/usr/bin/env python3
"""Tests for Maui market intelligence helpers."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from maui_market.analysis import (  # noqa: E402
    collect_diff_events,
    diff_snapshots,
    estimate_value,
    summarize_market,
)
from maui_market.config import load_complex_config  # noqa: E402
from maui_market.db import (  # noqa: E402
    connect,
    get_snapshot_listings,
    insert_snapshot,
    snapshot_count,
)
from maui_market.models import Listing  # noqa: E402
from maui_market.scraper.redfin import (  # noqa: E402
    listing_from_home_dict,
    parse_detail_page,
    parse_listing_price,
    parse_next_data,
    parse_price,
)
from maui_market.units import (  # noqa: E402
    apply_listing_identity,
    listing_matches_complex,
    load_unit_registry,
    normalize_unit_code,
    parse_unit,
    parse_unit_from_url,
)

CONFIG = load_complex_config("maui_kamaole")

SAMPLE_NEXT_DATA = """
<html><body>
<script id="__NEXT_DATA__" type="application/json">
{
  "props": {
    "pageProps": {
      "homes": [
        {
          "propertyId": 123456,
          "url": "/HI/Kihei/Maui-Kamaole/2777-S-Kihei-Rd-Unit-M109/home/123456",
          "streetLine": "2777 S Kihei Rd Unit M109",
          "price": 799000,
          "beds": 2,
          "baths": 2,
          "sqFt": 849,
          "status": "Active"
        }
      ]
    }
  }
}
</script></body></html>
"""

SAMPLE_DETAIL_HTML = """
<html><body>
<script>
{"mlsStatus":"Pending","sqFt":849,"beds":2,"baths":2,"price":799000,
"marketingRemarks":"Spacious Maui Kamaole unit with ocean views."}
</script>
<img src="https://ssl.cdn-redfin.com/photo/1.jpg" />
<img src="https://ssl.cdn-redfin.com/photo/2.jpg" />
</body></html>
"""


def make_listing(
    listing_id: str,
    *,
    price: int = 700000,
    status: str = "active",
    unit: str = "M109",
) -> Listing:
    listing = Listing(
        listing_id=listing_id,
        unit=unit,
        address=f"2777 S Kihei Rd Unit {unit}",
        price=price,
        bedrooms=2,
        bathrooms=2,
        sqft=849,
        price_per_sqft=round(price / 849, 2),
        status=status,
        listing_url=f"https://www.redfin.com/home/{listing_id}",
    )
    return listing


class ParserTests(unittest.TestCase):
    def test_parse_price(self) -> None:
        self.assertEqual(parse_price("$799,000"), 799000)
        self.assertIsNone(parse_price(""))

    def test_parse_unit(self) -> None:
        self.assertEqual(
            parse_unit("2777 S Kihei Rd Unit M109", CONFIG.address_pattern),
            "M109",
        )
        self.assertEqual(
            parse_unit("2777 S Kihei Rd Apt L-105", CONFIG.address_pattern),
            "L105",
        )

    def test_parse_next_data(self) -> None:
        listings = parse_next_data(SAMPLE_NEXT_DATA, CONFIG)
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].listing_id, "123456")
        self.assertEqual(listings[0].unit, "M109")
        self.assertEqual(listings[0].price, 799000)

    def test_listing_from_home_dict(self) -> None:
        home = {
            "url": "/HI/Kihei/home/999",
            "streetLine": "2777 S Kihei Rd Unit G201",
            "price": 650000,
            "beds": 1,
            "baths": 1,
            "sqFt": 700,
            "status": "Active",
        }
        listing = listing_from_home_dict(home, CONFIG)
        assert listing is not None
        self.assertEqual(listing.unit, "G201")
        self.assertAlmostEqual(listing.price_per_sqft or 0, 928.57, places=2)

    def test_parse_detail_page(self) -> None:
        description, photos, status = parse_detail_page(SAMPLE_DETAIL_HTML)
        self.assertIn("Maui Kamaole", description)
        self.assertEqual(len(photos), 2)
        self.assertEqual(status, "pending")

    def test_parse_unit_from_url(self) -> None:
        url = (
            "https://www.redfin.com/HI/Kihei/Maui-Kamaole/"
            "2777-S-Kihei-Rd-96753/unit-B104/home/123456"
        )
        self.assertEqual(parse_unit_from_url(url), "B104")

    def test_listing_matches_complex_filters_wrong_street(self) -> None:
        wrong = Listing(
            listing_id="999",
            unit="101",
            address="",
            price=1000000,
            bedrooms=2,
            bathrooms=2,
            sqft=800,
            price_per_sqft=None,
            status="active",
            listing_url="https://www.redfin.com/HI/Kihei/2757-S-Kihei-Rd/unit-101/home/999",
        )
        right = Listing(
            listing_id="123",
            unit="B104",
            address="",
            price=800000,
            bedrooms=2,
            bathrooms=2,
            sqft=800,
            price_per_sqft=None,
            status="active",
            listing_url="https://www.redfin.com/HI/Kihei/2777-S-Kihei-Rd/unit-B104/home/123",
        )
        self.assertFalse(listing_matches_complex(wrong, CONFIG))
        self.assertTrue(listing_matches_complex(right, CONFIG))

    def test_apply_listing_identity_prefers_url_unit(self) -> None:
        listing = Listing(
            listing_id="123",
            unit="",
            address="",
            price=800000,
            bedrooms=2,
            bathrooms=2,
            sqft=800,
            price_per_sqft=None,
            status="active",
            listing_url="https://www.redfin.com/HI/Kihei/2777-S-Kihei-Rd/unit-C208/home/123",
            description="Lovely L-105 with ocean views",
        )
        resolved = apply_listing_identity(listing, CONFIG)
        assert resolved is not None
        self.assertEqual(resolved.unit, "C208")

    def test_load_unit_registry_from_county_tmks(self) -> None:
        registry = load_unit_registry(CONFIG)
        assert registry is not None
        self.assertGreater(len(registry.units), 100)
        self.assertIn("G101", registry.units)
        self.assertIn("M109", registry.units)

    def test_parse_listing_price_ignores_noise(self) -> None:
        html = '{"price": 1287, "listPrice": 799000}'
        self.assertEqual(parse_listing_price(html), 799000)


class AnalysisTests(unittest.TestCase):
    def test_summarize_market(self) -> None:
        listings = [
            make_listing("1", price=700000),
            make_listing("2", price=800000, unit="L105"),
        ]
        summary = summarize_market(listings)
        self.assertEqual(summary.listing_count, 2)
        self.assertEqual(summary.median_price, 750000)

    def test_diff_new_removed_and_reduction(self) -> None:
        previous = [make_listing("1", price=800000), make_listing("2", price=700000)]
        current = [make_listing("1", price=750000), make_listing("3", price=690000, unit="G201")]
        diff = diff_snapshots(
            current,
            previous,
            current_snapshot_id=2,
            previous_snapshot_id=1,
        )
        self.assertEqual(len(diff.new_listings), 1)
        self.assertEqual(diff.new_listings[0].listing_id, "3")
        self.assertEqual(len(diff.removed_listings), 1)
        self.assertEqual(diff.removed_listings[0].listing_id, "2")
        self.assertEqual(len(diff.price_reductions), 1)
        self.assertEqual(diff.price_reductions[0].listing_id, "1")

    def test_diff_pending_status(self) -> None:
        previous = [make_listing("1", status="active")]
        current = [make_listing("1", status="pending")]
        diff = diff_snapshots(
            current,
            previous,
            current_snapshot_id=2,
            previous_snapshot_id=1,
        )
        self.assertEqual(len(diff.status_pending), 1)

    def test_estimate_value(self) -> None:
        listings = [
            make_listing("1", price=800000),
            make_listing("2", price=700000, unit="L105"),
        ]
        estimate = estimate_value(listings, bedrooms=2, sqft=850)
        self.assertIsNotNone(estimate["estimated_price"])
        self.assertGreater(estimate["comp_count"], 0)

    def test_collect_diff_events(self) -> None:
        diff = diff_snapshots(
            [make_listing("1")],
            [],
            current_snapshot_id=1,
            previous_snapshot_id=None,
        )
        events = collect_diff_events(diff)
        self.assertTrue(any(event.event_type == "new" for event in events))


class DatabaseTests(unittest.TestCase):
    def test_db_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = connect(db_path)
            listings = [make_listing("123456")]
            snapshot_id = insert_snapshot(
                conn,
                "maui_kamaole",
                "redfin",
                listings,
                datetime(2026, 7, 6, 12, 0, 0),
            )
            self.assertEqual(snapshot_count(conn, "maui_kamaole"), 1)
            loaded = get_snapshot_listings(conn, snapshot_id)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].listing_id, "123456")
            self.assertEqual(json.loads(json.dumps(loaded[0].photo_urls)), [])
            conn.close()


if __name__ == "__main__":
    unittest.main()
