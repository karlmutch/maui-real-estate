#!/usr/bin/env python3
"""Tests for homestead exemption reporting in ownership_timeline.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ownership_timeline import (  # noqa: E402
    UnitTaxRecord,
    build_homestead_exemption_summary_row,
    pct_of_total,
)


def make_record(
    *,
    tax_rate_class_code: str,
    land_exemption: int = 0,
    building_exemption: int = 0,
    owner_address_region: str = "hi",
    unit: str = "A 101",
) -> UnitTaxRecord:
    return UnitTaxRecord(
        tmk="239004082",
        cpr="0001",
        unit=unit,
        parid="390040820001",
        tax_rate_class_code=tax_rate_class_code,
        tax_rate_class_label="",
        land_class_code=tax_rate_class_code,
        land_exemption=land_exemption,
        building_exemption=building_exemption,
        owner_address_region=owner_address_region,
    )


class HomesteadExemptionTests(unittest.TestCase):
    def test_homestead_requires_owner_occupied_and_exemption(self) -> None:
        homestead = make_record(tax_rate_class_code="9", building_exemption=300000)
        owner_no_exemption = make_record(tax_rate_class_code="9")
        strh = make_record(tax_rate_class_code="11", building_exemption=300000)

        self.assertTrue(homestead.is_homestead_exemption)
        self.assertFalse(owner_no_exemption.is_homestead_exemption)
        self.assertFalse(strh.is_homestead_exemption)

    def test_ltr_exemption_is_separate_from_homestead(self) -> None:
        ltr = make_record(tax_rate_class_code="12", building_exemption=200000)
        self.assertTrue(ltr.is_ltr_exemption)
        self.assertFalse(ltr.is_homestead_exemption)

    def test_build_homestead_exemption_summary_row(self) -> None:
        records = [
            make_record(tax_rate_class_code="9", building_exemption=300000, unit="E 104"),
            make_record(
                tax_rate_class_code="9",
                building_exemption=300000,
                owner_address_region="usa",
                unit="G 110",
            ),
            make_record(tax_rate_class_code="12", building_exemption=200000, unit="D 104"),
            make_record(tax_rate_class_code="11", unit="D 101"),
        ]
        row = build_homestead_exemption_summary_row(records, "239004082")

        self.assertEqual(row["total_units"], "4")
        self.assertEqual(row["homestead_units"], "2")
        self.assertEqual(row["homestead_pct"], "50.0000")
        self.assertEqual(row["homestead_hi_units"], "1")
        self.assertEqual(row["homestead_usa_units"], "1")
        self.assertEqual(row["ltr_exemption_units"], "1")
        self.assertEqual(row["no_exemption_units"], "1")

    def test_pct_of_total(self) -> None:
        self.assertEqual(pct_of_total(14, 316), 4.4304)
        self.assertEqual(pct_of_total(0, 0), 0.0)


if __name__ == "__main__":
    unittest.main()
