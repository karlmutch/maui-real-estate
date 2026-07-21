#!/usr/bin/env python3
"""Offline tests for BOC mortgage lookup helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from maui_market.parcel_index import (  # noqa: E402
    ParcelUnit,
    build_building_phase_map,
    build_tmk_phase_counts,
    format_boc_tmk,
    format_boc_unit,
    format_street_address,
    load_parcel_units,
    parse_condominium_name,
    parcel_unit_from_row,
)
from maui_market.scraper.boc import BocDocument, parse_results_html  # noqa: E402
from mortgage_lookup import (  # noqa: E402
    MAUI_KAMAOLE_BOC_NAME_CHAIN,
    classify_mortgage_status,
    clear_unit_cache,
    condominium_names_to_try,
    pending_units,
    search_unit_documents,
    store_unit_documents,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

SAMPLE_ROW = {
    "DIVISION - TMK": "2",
    "ZONE - TMK": "3",
    "SECTION - TMK": "9",
    "PLAT - TMK": "004",
    "PARCEL - TMK": "082",
    "CPR - TMK": "0001",
    "STREET NUMBER": "2777",
    "STREET DIRECTION": "S",
    "STREET": "KIHEI",
    "STREET NAME SUFFIX": "RD",
    "UNIT": "G 101",
}


class ParcelIndexTests(unittest.TestCase):
    def test_format_boc_tmk(self) -> None:
        self.assertEqual(
            format_boc_tmk("2", "3", "9", "004", "082", "0001"),
            "2-3-9-004-082-0001",
        )

    def test_format_boc_unit(self) -> None:
        self.assertEqual(format_boc_unit("G 101"), "G101")
        self.assertEqual(format_boc_unit("H-205"), "H205")

    def test_parse_condominium_name(self) -> None:
        self.assertEqual(
            parse_condominium_name("APT NO G-108 MAUI KAMAOLE PHASE III (AS GIVEN)"),
            "MAUI KAMAOLE PHASE III",
        )
        self.assertEqual(
            parse_condominium_name("APT B-206 MAUI KAMAOLE PHASE II (AS GIVEN)"),
            "MAUI KAMAOLE PHASE II",
        )
        self.assertEqual(
            parse_condominium_name("APT G-101 MAUI KAMAOLE CM 1146"),
            "MAUI KAMAOLE",
        )
        self.assertEqual(
            parse_condominium_name(
                "APT G-101 MAUI KAMAOLE CM 1146",
                unit="G 101",
                tmk_key="239004082",
                building_phase_map={("239004082", "G"): "MAUI KAMAOLE PHASE III"},
            ),
            "MAUI KAMAOLE PHASE III",
        )
        self.assertEqual(
            parse_condominium_name(
                "APT A-101 MAUI KAMAOLE CM 1146",
                unit="A 101",
                tmk_key="239004143",
                tmk_phase_counts={"239004143": {"MAUI KAMAOLE PHASE III": 1}},
            ),
            "MAUI KAMAOLE",
        )
        self.assertEqual(
            parse_condominium_name(
                "APT A-210 MAUI KAMAOLE PHASE III (AS GIVEN)",
                unit="A 210",
                tmk_key="239004143",
                tmk_phase_counts={"239004143": {"MAUI KAMAOLE PHASE III": 1}},
            ),
            "MAUI KAMAOLE",
        )
        self.assertEqual(
            parse_condominium_name("APT A-101 MAUI KAMAOLE (LC) CM 1146"),
            "MAUI KAMAOLE (LC)",
        )

    def test_build_building_phase_map(self) -> None:
        legal = {
            ("239004082", "0008"): "APT NO G-108 MAUI KAMAOLE PHASE III (AS GIVEN)",
            ("239004082", "0017"): "APT NO G-203 MAUI KAMAOLE PHASE III",
            ("239004144", "0020"): "APT B-206 MAUI KAMAOLE PHASE II (AS GIVEN)",
            ("239004144", "0023"): "APT B-209 MAUI KAMAOLE PH II (AS GIVEN)",
        }
        phase_map = build_building_phase_map(legal)
        self.assertEqual(phase_map[("239004082", "G")], "MAUI KAMAOLE PHASE III")
        self.assertEqual(phase_map[("239004144", "B")], "MAUI KAMAOLE PHASE II")

    def test_format_street_address(self) -> None:
        self.assertEqual(
            format_street_address(SAMPLE_ROW),
            "2777 S KIHEI RD UNIT G 101",
        )

    def test_parcel_unit_from_row(self) -> None:
        legal = {
            ("239004082", "0001"): "APT G-101 MAUI KAMAOLE CM 1146",
            ("239004082", "0008"): "APT NO G-108 MAUI KAMAOLE PHASE III (AS GIVEN)",
            ("239004082", "0017"): "APT NO G-203 MAUI KAMAOLE PHASE III",
        }
        phase_map = build_building_phase_map(legal)
        tmk_phase_counts = build_tmk_phase_counts(legal)
        unit = parcel_unit_from_row(
            SAMPLE_ROW, "239004082", legal, phase_map, tmk_phase_counts
        )
        assert unit is not None
        self.assertEqual(unit.boc_tmk, "2-3-9-004-082-0001")
        self.assertEqual(unit.boc_unit, "G101")
        self.assertEqual(unit.condominium_name, "MAUI KAMAOLE PHASE III")

    def test_load_parcel_units_maui_kamaole(self) -> None:
        units = load_parcel_units(ROOT / "data" / "maui-kamaole.tmks", ROOT / "data")
        self.assertGreater(len(units), 200)
        self.assertTrue(all(unit.cpr != "0000" for unit in units))
        self.assertTrue(all(unit.boc_unit for unit in units))
        phase_three = [unit for unit in units if unit.condominium_name.endswith("PHASE III")]
        self.assertGreater(len(phase_three), 0)
        g101 = next(unit for unit in units if unit.boc_unit == "G101")
        self.assertEqual(g101.condominium_name, "MAUI KAMAOLE PHASE III")
        a101 = next(unit for unit in units if unit.boc_unit == "A101")
        self.assertEqual(a101.condominium_name, "MAUI KAMAOLE")
        a210 = next(unit for unit in units if unit.boc_unit == "A210")
        self.assertEqual(a210.condominium_name, "MAUI KAMAOLE")
        a_units = [unit for unit in units if unit.boc_unit.startswith("A")]
        self.assertTrue(all(unit.condominium_name == "MAUI KAMAOLE" for unit in a_units))


class BocParserTests(unittest.TestCase):
    def test_parse_results_released_mortgage(self) -> None:
        html = (FIXTURES / "boc_results_released.html").read_text(encoding="utf-8")
        documents = parse_results_html(html, "2-3-9-004-082-0001")
        codes = {doc.instrument_code for doc in documents}
        self.assertIn("M", codes)
        self.assertIn("R", codes)
        self.assertIn("NL", codes)

    def test_parse_results_open_mortgage(self) -> None:
        html = (FIXTURES / "boc_results_open.html").read_text(encoding="utf-8")
        documents = parse_results_html(html, "2-3-9-004-082-0002")
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].instrument_code, "MFS")

    def test_parse_results_slash_instrument_codes(self) -> None:
        html = (FIXTURES / "boc_results_slash_codes.html").read_text(encoding="utf-8")
        documents = parse_results_html(html, "2-3-9-004-082-0064")
        by_number = {doc.document_number: doc for doc in documents}
        self.assertNotIn("Reference Documents", by_number)
        self.assertEqual(by_number["A2004111655"].instrument_code, "M")
        self.assertEqual(by_number["54430338"].instrument_code, "M")
        self.assertEqual(by_number["A65330972"].instrument_code, "R")


class MauiKamaoleFallbackTests(unittest.TestCase):
    def _unit(self, condominium_name: str, boc_unit: str = "G101") -> ParcelUnit:
        return ParcelUnit(
            tmk_key="239004082",
            cpr="0001",
            parid="390040820001",
            boc_tmk="2-3-9-004-082-0001",
            unit=boc_unit,
            boc_unit=boc_unit,
            condominium_name=condominium_name,
            street_address="2777 S KIHEI RD",
            division="2",
            zone="3",
            section="9",
            plat="004",
            parcel="082",
        )

    def test_condominium_names_to_try_phase_three(self) -> None:
        names = condominium_names_to_try(self._unit("MAUI KAMAOLE PHASE III"))
        self.assertEqual(names, list(MAUI_KAMAOLE_BOC_NAME_CHAIN))

    def test_condominium_names_to_try_plain_maui_kamaole(self) -> None:
        names = condominium_names_to_try(self._unit("MAUI KAMAOLE", boc_unit="A101"))
        self.assertEqual(
            names,
            ["MAUI KAMAOLE", *MAUI_KAMAOLE_BOC_NAME_CHAIN],
        )

    def test_condominium_names_to_try_phase_two(self) -> None:
        names = condominium_names_to_try(self._unit("MAUI KAMAOLE PHASE II", boc_unit="B206"))
        self.assertEqual(
            names,
            [
                "MAUI KAMAOLE PHASE II",
                "MAUI KAMAOLE PHASE III",
                "MAUI KAMAOLE (LC)",
                "MAUI KAMAOLE (RS)",
            ],
        )

    def test_search_unit_documents_tries_fallback_names(self) -> None:
        unit = self._unit("MAUI KAMAOLE PHASE III")
        calls: list[str] = []

        class FakeScraper:
            def search_condominium(self, _driver, condominium_name, _boc_unit, _boc_tmk):
                calls.append(condominium_name)
                if condominium_name == "MAUI KAMAOLE (LC)":
                    return [
                        BocDocument(
                            boc_tmk=unit.boc_tmk,
                            document_number="LC-1",
                            instrument_code="M",
                        )
                    ]
                return []

        documents, matched_name = search_unit_documents(FakeScraper(), None, unit)
        self.assertEqual(
            calls,
            [
                "MAUI KAMAOLE PHASE III",
                "MAUI KAMAOLE PHASE II",
                "MAUI KAMAOLE (LC)",
            ],
        )
        self.assertEqual(matched_name, "MAUI KAMAOLE (LC)")
        self.assertEqual(len(documents), 1)

    def test_search_unit_documents_a_unit_tries_lc_after_base_name(self) -> None:
        unit = self._unit("MAUI KAMAOLE", boc_unit="A101")
        calls: list[str] = []

        class FakeScraper:
            def search_condominium(self, _driver, condominium_name, _boc_unit, _boc_tmk):
                calls.append(condominium_name)
                if condominium_name == "MAUI KAMAOLE (LC)":
                    return [
                        BocDocument(
                            boc_tmk=unit.boc_tmk,
                            document_number="LC-1",
                            instrument_code="M",
                        )
                    ]
                return []

        documents, matched_name = search_unit_documents(FakeScraper(), None, unit)
        self.assertEqual(
            calls,
            [
                "MAUI KAMAOLE",
                "MAUI KAMAOLE PHASE III",
                "MAUI KAMAOLE PHASE II",
                "MAUI KAMAOLE (LC)",
            ],
        )
        self.assertEqual(matched_name, "MAUI KAMAOLE (LC)")
        self.assertEqual(len(documents), 1)

    def test_search_unit_documents_returns_empty_after_exhausting_names(self) -> None:
        unit = self._unit("MAUI KAMAOLE PHASE III")

        class FakeScraper:
            def search_condominium(self, _driver, _condominium_name, _boc_unit, _boc_tmk):
                return []

        documents, matched_name = search_unit_documents(FakeScraper(), None, unit)
        self.assertEqual(documents, [])
        self.assertEqual(matched_name, "MAUI KAMAOLE (RS)")


class CacheUpdateTests(unittest.TestCase):
    def test_store_unit_documents_caches_non_empty_results(self) -> None:
        cache: dict[str, list[dict[str, str]]] = {}
        documents = [
            BocDocument(
                boc_tmk="2-3-9-004-082-0001",
                document_number="123",
                instrument_code="M",
            )
        ]
        self.assertTrue(store_unit_documents(cache, "2-3-9-004-082-0001", documents))
        self.assertEqual(len(cache["2-3-9-004-082-0001"]), 1)

    def test_store_unit_documents_clears_cache_on_empty_results(self) -> None:
        cache = {
            "2-3-9-004-082-0001": [{"document_number": "123"}],
            "2-3-9-004-082-0001__error": [{"search_error": "stale element"}],
        }
        self.assertFalse(store_unit_documents(cache, "2-3-9-004-082-0001", []))
        self.assertNotIn("2-3-9-004-082-0001", cache)
        self.assertNotIn("2-3-9-004-082-0001__error", cache)

    def test_clear_unit_cache_removes_documents_and_errors(self) -> None:
        cache = {
            "2-3-9-004-082-0002": [{"document_number": "456"}],
            "2-3-9-004-082-0002__error": [{"search_error": "timeout"}],
        }
        clear_unit_cache(cache, "2-3-9-004-082-0002")
        self.assertEqual(cache, {})


class ResumeCacheTests(unittest.TestCase):
    def _unit(self, boc_tmk: str, boc_unit: str) -> ParcelUnit:
        return ParcelUnit(
            tmk_key="239004082",
            cpr="0001",
            parid="390040820001",
            boc_tmk=boc_tmk,
            unit=boc_unit,
            boc_unit=boc_unit,
            condominium_name="MAUI KAMAOLE PHASE III",
            street_address="2777 S KIHEI RD",
            division="2",
            zone="3",
            section="9",
            plat="004",
            parcel="082",
        )

    def test_pending_units_without_resume(self) -> None:
        units = [self._unit("2-3-9-004-082-0001", "G101")]
        cache = {"2-3-9-004-082-0001": [{"document_number": "123"}]}
        self.assertEqual(pending_units(units, cache, resume=False), units)

    def test_pending_units_resume_skips_cached_documents(self) -> None:
        units = [self._unit("2-3-9-004-082-0001", "G101")]
        cache = {"2-3-9-004-082-0001": [{"document_number": "123"}]}
        self.assertEqual(pending_units(units, cache, resume=True), [])

    def test_pending_units_resume_retries_missing_and_zero_document(self) -> None:
        units = [
            self._unit("2-3-9-004-082-0001", "G101"),
            self._unit("2-3-9-004-082-0002", "G102"),
            self._unit("2-3-9-004-082-0003", "G103"),
        ]
        cache = {
            "2-3-9-004-082-0001": [{"document_number": "123"}],
            "2-3-9-004-082-0002": [],
            "2-3-9-004-082-0002__error": [{"search_error": "stale element"}],
        }
        pending = pending_units(units, cache, resume=True)
        self.assertEqual([unit.boc_unit for unit in pending], ["G102", "G103"])


class MortgageStatusTests(unittest.TestCase):
    def _unit(self) -> ParcelUnit:
        return ParcelUnit(
            tmk_key="239004082",
            cpr="0001",
            parid="390040820001",
            boc_tmk="2-3-9-004-082-0001",
            unit="G 101",
            boc_unit="G101",
            condominium_name="MAUI KAMAOLE",
            street_address="2777 S KIHEI RD UNIT G 101",
            division="2",
            zone="3",
            section="9",
            plat="004",
            parcel="082",
        )

    def test_status_none(self) -> None:
        status = classify_mortgage_status(self._unit(), [])
        self.assertEqual(status.mortgage_status, "none")

    def test_status_likely_open(self) -> None:
        documents = [
            BocDocument(
                boc_tmk="2-3-9-004-082-0002",
                document_number="2019-998877",
                instrument_code="MFS",
                recording_date="11/02/2019",
            )
        ]
        status = classify_mortgage_status(self._unit(), documents)
        self.assertEqual(status.mortgage_status, "likely_open")

    def test_status_likely_released(self) -> None:
        documents = [
            BocDocument(
                boc_tmk="2-3-9-004-082-0001",
                instrument_code="M",
                recording_date="03/15/2020",
            ),
            BocDocument(
                boc_tmk="2-3-9-004-082-0001",
                instrument_code="R",
                recording_date="08/01/2023",
            ),
            BocDocument(
                boc_tmk="2-3-9-004-082-0001",
                instrument_code="NL",
                recording_date="01/10/2024",
            ),
        ]
        status = classify_mortgage_status(self._unit(), documents)
        self.assertEqual(status.mortgage_status, "likely_released")
        self.assertTrue(status.has_notice_of_lien)

    def test_status_unknown_on_error(self) -> None:
        status = classify_mortgage_status(self._unit(), [], search_error="timeout")
        self.assertEqual(status.mortgage_status, "unknown")


if __name__ == "__main__":
    unittest.main()
