#!/usr/bin/env python3
"""Tests for owner residency classification helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from county_metadata import (
    OwnerAddressEntry,
    OwnerResidencyResult,
    address_region_shares,
    classify_owner_address_region,
    classify_owner_residency,
    classify_owner_residency_detail,
    effective_owner_mailing_fields,
    is_long_term_rental_tax_class,
    is_owner_occupied_tax_class,
    parse_exemption_amount,
    parse_ownraddr_line,
    resolve_owner_residencies,
    resolve_owner_residency_details,
    resolve_tmk_owner_residency_details,
    residency_shares,
)


class ClassifyOwnerResidencyTests(unittest.TestCase):
    def test_hi_state(self) -> None:
        result = classify_owner_residency_detail("HI", "KIHEI HI 96753", "")
        self.assertEqual(result.residency, "hi")
        self.assertEqual(result.confidence, "high")

    def test_non_hi_state(self) -> None:
        self.assertEqual(classify_owner_residency("CA", "SOQUEL CA 95073", ""), "non_hi")

    def test_foreign_country(self) -> None:
        self.assertEqual(classify_owner_residency("", "VICTORIA BC", "CANADA"), "non_hi")

    def test_hi_in_city_state_zip(self) -> None:
        self.assertEqual(classify_owner_residency("", "KIHEI HI 96753", ""), "hi")

    def test_hi_zip_without_state_token(self) -> None:
        result = classify_owner_residency_detail("", "KIHEI 96753", "")
        self.assertEqual(result.residency, "hi")
        self.assertEqual(result.confidence, "inferred")

    def test_non_hi_city_without_hi_token(self) -> None:
        self.assertEqual(classify_owner_residency("", "PLEASANT HILL CA 94523", ""), "non_hi")

    def test_blank_address(self) -> None:
        result = classify_owner_residency_detail("", "", "")
        self.assertEqual(result.residency, "unknown")
        self.assertEqual(result.confidence, "unknown")


class ResolveOwnerResidencyTests(unittest.TestCase):
    def test_conservative_default_does_not_infer(self) -> None:
        raw = ["non_hi", "unknown"]
        self.assertEqual(resolve_owner_residencies(raw), ["non_hi", "unknown"])

    def test_infer_when_enabled_and_known_agree(self) -> None:
        raw = ["non_hi", "unknown"]
        self.assertEqual(
            resolve_owner_residencies(raw, infer_coowner_residency=True),
            ["non_hi", "non_hi"],
        )

    def test_infer_marks_confidence_inferred(self) -> None:
        details = [
            OwnerResidencyResult("non_hi", "high"),
            OwnerResidencyResult("unknown", "unknown"),
        ]
        resolved = resolve_owner_residency_details(details, infer_coowner_residency=True)
        self.assertEqual(resolved[1].residency, "non_hi")
        self.assertEqual(resolved[1].confidence, "inferred")

    def test_mixed_known_residencies_leave_unknowns(self) -> None:
        details = [
            OwnerResidencyResult("hi", "high"),
            OwnerResidencyResult("non_hi", "high"),
            OwnerResidencyResult("unknown", "unknown"),
        ]
        resolved = resolve_owner_residency_details(details, infer_coowner_residency=True)
        self.assertEqual([item.residency for item in resolved], ["hi", "non_hi", "unknown"])


class ResidencyShareTests(unittest.TestCase):
    def test_mixed_unit_shares(self) -> None:
        shares = residency_shares(["hi", "non_hi"])
        self.assertEqual(shares["hi"], 50.0)
        self.assertEqual(shares["non_hi"], 50.0)
        self.assertEqual(shares["unknown"], 0.0)

    def test_all_unknown_unit(self) -> None:
        shares = residency_shares(["unknown", "unknown", "unknown"])
        self.assertEqual(shares["unknown"], 100.0)


class TmkResidencyFallbackTests(unittest.TestCase):
    def test_same_unit_co_owner_fallback(self) -> None:
        entries = [
            OwnerAddressEntry(
                "0002",
                "BARTLE,JEFFREY K TRUST",
                "OR",
                "PORTLAND OR 97229",
                "",
                classify_owner_residency_detail("OR", "PORTLAND OR 97229", ""),
            ),
            OwnerAddressEntry(
                "0002",
                "BARTLE,LINDA K TRUST",
                "",
                "",
                "",
                classify_owner_residency_detail("", "", ""),
            ),
        ]
        resolved = resolve_tmk_owner_residency_details(entries)
        self.assertEqual(resolved[("0002", "BARTLE,LINDA K TRUST")].residency, "non_hi")
        self.assertEqual(resolved[("0002", "BARTLE,LINDA K TRUST")].confidence, "inferred")

    def test_same_owner_name_across_units(self) -> None:
        entries = [
            OwnerAddressEntry(
                "0041",
                "SHAW,WILLIAM & WANNEE TR",
                "TX",
                "FORT WORTH TX 76123",
                "",
                classify_owner_residency_detail("TX", "FORT WORTH TX 76123", ""),
            ),
            OwnerAddressEntry(
                "0042",
                "SHAW,WILLIAM & WANNEE TR",
                "",
                "",
                "",
                classify_owner_residency_detail("", "", ""),
            ),
        ]
        resolved = resolve_tmk_owner_residency_details(entries)
        self.assertEqual(resolved[("0042", "SHAW,WILLIAM & WANNEE TR")].residency, "non_hi")

    def test_mixed_clear_residencies_do_not_fill(self) -> None:
        entries = [
            OwnerAddressEntry(
                "0141",
                "CUMMING,WILLIAM GORDON JR",
                "HI",
                "KIHEI HI 96753",
                "",
                classify_owner_residency_detail("HI", "KIHEI HI 96753", ""),
            ),
            OwnerAddressEntry(
                "0141",
                "CUMMING,WILLIAM GORDON III",
                "MD",
                "EDGEWATER MD 21037",
                "",
                classify_owner_residency_detail("MD", "EDGEWATER MD 21037", ""),
            ),
            OwnerAddressEntry(
                "0141",
                "CUMMING,OTHER",
                "",
                "",
                "",
                classify_owner_residency_detail("", "", ""),
            ),
        ]
        resolved = resolve_tmk_owner_residency_details(entries)
        self.assertEqual(resolved[("0141", "CUMMING,OTHER")].residency, "unknown")


class OwnraddrTests(unittest.TestCase):
    def test_parse_ownraddr_line(self) -> None:
        ownraddr_path = Path(__file__).resolve().parent.parent / "data" / "ownership-data" / "ownraddr.txt"
        if not ownraddr_path.is_file():
            self.skipTest("ownraddr.txt not available")
        with ownraddr_path.open(encoding="utf-8", errors="replace") as handle:
            line = next(
                raw_line.rstrip("\n")
                for raw_line in handle
                if raw_line.startswith("2390040820002BARTLE,JEFFREY")
            )
        parsed = parse_ownraddr_line(line)
        self.assertIsNotNone(parsed)
        prefix, owner, state, city_state_zip, _country = parsed
        self.assertEqual(prefix, "2390040820002")
        self.assertEqual(owner, "BARTLE,JEFFREY K TRUST")
        self.assertEqual(state, "OR")
        self.assertIn("PORTLAND OR 97229", city_state_zip)

    def test_effective_owner_mailing_fields_uses_supplement(self) -> None:
        row = {"MAILING STATE": "", "MAILING CITY STATE ZIP": "", "COUNTRY": ""}
        supplement = {
            ("0002", "BARTLE,LINDA K TRUST"): {
                "mailing_state": "OR",
                "mailing_city_state_zip": "PORTLAND OR 97229",
                "country": "",
            }
        }
        state, city_state_zip, country = effective_owner_mailing_fields(
            "0002",
            "BARTLE,LINDA K TRUST",
            row,
            supplement,
        )
        self.assertEqual(state, "OR")
        self.assertEqual(city_state_zip, "PORTLAND OR 97229")


class ClassifyOwnerAddressRegionTests(unittest.TestCase):
    def test_hi_state(self) -> None:
        self.assertEqual(classify_owner_address_region("HI", "KIHEI HI 96753", ""), "hi")

    def test_usa_state(self) -> None:
        self.assertEqual(classify_owner_address_region("CA", "SOQUEL CA 95073", ""), "usa")

    def test_foreign_country(self) -> None:
        self.assertEqual(classify_owner_address_region("", "VICTORIA BC", "CANADA"), "foreign")

    def test_address_region_shares(self) -> None:
        shares = address_region_shares(["hi", "usa"])
        self.assertEqual(shares["hi"], 50.0)
        self.assertEqual(shares["usa"], 50.0)


class ExemptionHelperTests(unittest.TestCase):
    def test_parse_exemption_amount(self) -> None:
        self.assertEqual(parse_exemption_amount(""), 0)
        self.assertEqual(parse_exemption_amount("0"), 0)
        self.assertEqual(parse_exemption_amount("200000"), 200000)
        self.assertEqual(parse_exemption_amount("$300,000"), 300000)

    def test_is_owner_occupied_tax_class(self) -> None:
        self.assertTrue(is_owner_occupied_tax_class("9"))
        self.assertTrue(is_owner_occupied_tax_class("09"))
        self.assertFalse(is_owner_occupied_tax_class("11"))
        self.assertFalse(is_owner_occupied_tax_class(""))

    def test_is_long_term_rental_tax_class(self) -> None:
        self.assertTrue(is_long_term_rental_tax_class("12"))
        self.assertTrue(is_long_term_rental_tax_class("012"))
        self.assertFalse(is_long_term_rental_tax_class("9"))
        self.assertFalse(is_long_term_rental_tax_class(""))


if __name__ == "__main__":
    unittest.main()
