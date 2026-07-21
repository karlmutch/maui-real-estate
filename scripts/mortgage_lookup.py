#!/usr/bin/env python3
"""Query Hawaii Bureau of Conveyances for recorded mortgage instruments by TMK."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from maui_market.parcel_index import ParcelUnit, load_parcel_units  # noqa: E402
from maui_market.scraper.boc import (  # noqa: E402
    LIEN_CODE,
    MORTGAGE_CODES,
    RELEASE_CODE,
    BocDocument,
    BocScraper,
    document_to_dict,
)

logger = logging.getLogger(__name__)

STATUS_NONE = "none"
STATUS_LIKELY_OPEN = "likely_open"
STATUS_LIKELY_RELEASED = "likely_released"
STATUS_UNKNOWN = "unknown"

UNIT_FIELDS = (
    "tmk_key",
    "cpr",
    "parid",
    "boc_tmk",
    "unit",
    "boc_unit",
    "condominium_name",
    "street_address",
)

DOCUMENT_FIELDS = (
    "tmk_key",
    "cpr",
    "parid",
    "boc_tmk",
    "unit",
    "boc_unit",
    "condominium_name",
    "street_address",
    "document_number",
    "instrument_code",
    "recording_date",
    "grantor",
    "grantee",
    "description",
    "scraped_at",
)

STATUS_FIELDS = (
    "tmk_key",
    "cpr",
    "parid",
    "boc_tmk",
    "unit",
    "boc_unit",
    "condominium_name",
    "street_address",
    "has_mortgage_recorded",
    "has_release_recorded",
    "has_notice_of_lien",
    "latest_mortgage_date",
    "latest_release_date",
    "mortgage_status",
    "document_count",
    "search_error",
    "scraped_at",
)

SUMMARY_FIELDS = (
    "scope",
    "tmk_key",
    "mortgage_status",
    "unit_count",
)

MAUI_KAMAOLE_BOC_NAME_CHAIN = (
    "MAUI KAMAOLE PHASE III",
    "MAUI KAMAOLE PHASE II",
    "MAUI KAMAOLE (LC)",
    "MAUI KAMAOLE (RS)",
)


@dataclass
class MortgageStatus:
    unit: ParcelUnit
    has_mortgage_recorded: bool
    has_release_recorded: bool
    has_notice_of_lien: bool
    latest_mortgage_date: str
    latest_release_date: str
    mortgage_status: str
    document_count: int
    search_error: str = ""
    scraped_at: str = ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tmks", type=Path, required=True, help="Path to TMK key list")
    parser.add_argument("--data-root", type=Path, default=ROOT / "data", help="County data root")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "mortgage-qualified",
        help="Directory for output CSV files",
    )
    parser.add_argument("--username", default=os.environ.get("BOC_USERNAME"), help="BOC login email")
    parser.add_argument("--password", default=os.environ.get("BOC_PASSWORD"), help="BOC password")
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless")
    parser.add_argument("--limit", type=int, default=None, help="Maximum units to query")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip units with cached documents; re-fetch missing and zero-document units",
    )
    parser.add_argument(
        "--delay",
        type=float,
        nargs=2,
        metavar=("MIN", "MAX"),
        default=(2.0, 4.0),
        help="Delay range in seconds between BOC searches",
    )
    parser.add_argument("--dry-run", action="store_true", help="Build unit index only; no network")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv)


def parse_recording_date(value: str) -> date | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def latest_date(values: list[str]) -> str:
    dated = [(parse_recording_date(value), value) for value in values]
    valid = [(parsed, raw) for parsed, raw in dated if parsed is not None]
    if not valid:
        return max((raw for _, raw in dated if raw), default="")
    return max(valid, key=lambda item: item[0])[1]


def classify_mortgage_status(
    unit: ParcelUnit,
    documents: list[BocDocument],
    *,
    search_error: str = "",
) -> MortgageStatus:
    if search_error:
        return MortgageStatus(
            unit=unit,
            has_mortgage_recorded=False,
            has_release_recorded=False,
            has_notice_of_lien=False,
            latest_mortgage_date="",
            latest_release_date="",
            mortgage_status=STATUS_UNKNOWN,
            document_count=len(documents),
            search_error=search_error,
            scraped_at=datetime.now().isoformat(timespec="seconds"),
        )

    mortgage_dates = [
        doc.recording_date
        for doc in documents
        if doc.instrument_code.upper() in MORTGAGE_CODES and doc.recording_date
    ]
    release_dates = [
        doc.recording_date
        for doc in documents
        if doc.instrument_code.upper() == RELEASE_CODE and doc.recording_date
    ]
    has_mortgage = any(doc.instrument_code.upper() in MORTGAGE_CODES for doc in documents)
    has_release = any(doc.instrument_code.upper() == RELEASE_CODE for doc in documents)
    has_lien = any(doc.instrument_code.upper() == LIEN_CODE for doc in documents)
    latest_mortgage = latest_date(mortgage_dates)
    latest_release = latest_date(release_dates)

    if not has_mortgage:
        status = STATUS_NONE
    else:
        mortgage_parsed = parse_recording_date(latest_mortgage)
        release_parsed = parse_recording_date(latest_release)
        if not has_release or release_parsed is None:
            status = STATUS_LIKELY_OPEN
        elif mortgage_parsed is None:
            status = STATUS_LIKELY_OPEN
        elif release_parsed >= mortgage_parsed:
            status = STATUS_LIKELY_RELEASED
        else:
            status = STATUS_LIKELY_OPEN

    return MortgageStatus(
        unit=unit,
        has_mortgage_recorded=has_mortgage,
        has_release_recorded=has_release,
        has_notice_of_lien=has_lien,
        latest_mortgage_date=latest_mortgage,
        latest_release_date=latest_release,
        mortgage_status=status,
        document_count=len(documents),
        scraped_at=datetime.now().isoformat(timespec="seconds"),
    )


def cache_path(output_dir: Path, prefix: str) -> Path:
    return output_dir / ".cache" / f"{prefix}-boc-documents.json"


def load_cache(path: Path) -> dict[str, list[dict[str, str]]]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return payload


def save_cache(path: Path, payload: dict[str, list[dict[str, str]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def cache_error_key(boc_tmk: str) -> str:
    return f"{boc_tmk}__error"


def clear_unit_cache(cache: dict[str, list[dict[str, str]]], boc_tmk: str) -> None:
    cache.pop(boc_tmk, None)
    cache.pop(cache_error_key(boc_tmk), None)


def store_unit_documents(
    cache: dict[str, list[dict[str, str]]],
    boc_tmk: str,
    documents: list[BocDocument],
) -> bool:
    """Cache BOC documents when found; otherwise remove any stale cache entry."""
    if not documents:
        clear_unit_cache(cache, boc_tmk)
        return False
    cache[boc_tmk] = [document_to_dict(document) for document in documents]
    cache.pop(cache_error_key(boc_tmk), None)
    return True


def cached_document_count(cache: dict[str, list[dict[str, str]]], boc_tmk: str) -> int:
    rows = cache.get(boc_tmk)
    if not isinstance(rows, list):
        return 0
    return len(rows)


def pending_units(
    units: list[ParcelUnit],
    cache: dict[str, list[dict[str, str]]],
    *,
    resume: bool,
) -> list[ParcelUnit]:
    if not resume:
        return list(units)
    pending: list[ParcelUnit] = []
    for unit in units:
        if unit.boc_tmk not in cache or cached_document_count(cache, unit.boc_tmk) == 0:
            pending.append(unit)
    return pending


def is_maui_kamaole_unit(unit: ParcelUnit) -> bool:
    return unit.condominium_name.upper().startswith("MAUI KAMAOLE")


def condominium_names_to_try(unit: ParcelUnit) -> list[str]:
    """Return BOC condominium names to try, in order, for a unit."""
    if not is_maui_kamaole_unit(unit):
        return [unit.condominium_name]

    names: list[str] = []
    seen: set[str] = set()
    for name in (unit.condominium_name, *MAUI_KAMAOLE_BOC_NAME_CHAIN):
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def search_unit_documents(
    scraper: BocScraper,
    driver,
    unit: ParcelUnit,
) -> tuple[list[BocDocument], str | None]:
    """Search BOC for a unit, trying alternate Maui Kamaole names when needed."""
    names = condominium_names_to_try(unit)
    for index, name in enumerate(names):
        is_last = index == len(names) - 1
        try:
            documents = scraper.search_condominium(
                driver,
                name,
                unit.boc_unit,
                unit.boc_tmk,
            )
        except Exception as exc:  # noqa: BLE001 - try fallbacks
            if is_last:
                raise
            logger.warning(
                "BOC search failed for %s unit %s as %r: %s; trying next condominium name",
                unit.boc_tmk,
                unit.boc_unit,
                name,
                exc,
            )
            continue
        if documents:
            if name != unit.condominium_name:
                logger.info(
                    "BOC search succeeded for %s unit %s using alternate name %r (%d documents)",
                    unit.boc_tmk,
                    unit.boc_unit,
                    name,
                    len(documents),
                )
            return documents, name
        if is_last:
            return [], name
        logger.info(
            "BOC search returned no documents for %s unit %s as %r; trying next condominium name",
            unit.boc_tmk,
            unit.boc_unit,
            name,
        )
    return [], None


def documents_from_cache(rows: list[dict[str, str]], boc_tmk: str) -> list[BocDocument]:
    documents: list[BocDocument] = []
    for row in rows:
        documents.append(
            BocDocument(
                boc_tmk=boc_tmk,
                document_number=row.get("document_number", ""),
                instrument_code=row.get("instrument_code", ""),
                recording_date=row.get("recording_date", ""),
                grantor=row.get("grantor", ""),
                grantee=row.get("grantee", ""),
                description=row.get("description", ""),
            )
        )
    return documents


def write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def unit_rows(units: list[ParcelUnit]) -> list[dict[str, str]]:
    return [
        {
            "tmk_key": unit.tmk_key,
            "cpr": unit.cpr,
            "parid": unit.parid,
            "boc_tmk": unit.boc_tmk,
            "unit": unit.unit,
            "boc_unit": unit.boc_unit,
            "condominium_name": unit.condominium_name,
            "street_address": unit.street_address,
        }
        for unit in units
    ]


def document_rows(
    unit: ParcelUnit,
    documents: list[BocDocument],
    scraped_at: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for document in documents:
        rows.append(
            {
                "tmk_key": unit.tmk_key,
                "cpr": unit.cpr,
                "parid": unit.parid,
                "boc_tmk": unit.boc_tmk,
                "unit": unit.unit,
                "boc_unit": unit.boc_unit,
                "condominium_name": unit.condominium_name,
                "street_address": unit.street_address,
                "document_number": document.document_number,
                "instrument_code": document.instrument_code,
                "recording_date": document.recording_date,
                "grantor": document.grantor,
                "grantee": document.grantee,
                "description": document.description,
                "scraped_at": scraped_at,
            }
        )
    return rows


def status_row(status: MortgageStatus) -> dict[str, str]:
    return {
        "tmk_key": status.unit.tmk_key,
        "cpr": status.unit.cpr,
        "parid": status.unit.parid,
        "boc_tmk": status.unit.boc_tmk,
        "unit": status.unit.unit,
        "boc_unit": status.unit.boc_unit,
        "condominium_name": status.unit.condominium_name,
        "street_address": status.unit.street_address,
        "has_mortgage_recorded": "true" if status.has_mortgage_recorded else "false",
        "has_release_recorded": "true" if status.has_release_recorded else "false",
        "has_notice_of_lien": "true" if status.has_notice_of_lien else "false",
        "latest_mortgage_date": status.latest_mortgage_date,
        "latest_release_date": status.latest_release_date,
        "mortgage_status": status.mortgage_status,
        "document_count": str(status.document_count),
        "search_error": status.search_error,
        "scraped_at": status.scraped_at,
    }


def summary_rows(statuses: list[MortgageStatus]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    overall = Counter(status.mortgage_status for status in statuses)
    for mortgage_status, unit_count in sorted(overall.items()):
        rows.append(
            {
                "scope": "portfolio",
                "tmk_key": "",
                "mortgage_status": mortgage_status,
                "unit_count": str(unit_count),
            }
        )

    by_tmk: dict[str, list[MortgageStatus]] = {}
    for status in statuses:
        by_tmk.setdefault(status.unit.tmk_key, []).append(status)
    for tmk_key in sorted(by_tmk):
        counts = Counter(item.mortgage_status for item in by_tmk[tmk_key])
        for mortgage_status, unit_count in sorted(counts.items()):
            rows.append(
                {
                    "scope": "tmk",
                    "tmk_key": tmk_key,
                    "mortgage_status": mortgage_status,
                    "unit_count": str(unit_count),
                }
            )
    return rows


def print_summary(statuses: list[MortgageStatus]) -> None:
    counts = Counter(status.mortgage_status for status in statuses)
    print("BOC mortgage lookup summary")
    print(f"  units queried: {len(statuses)}")
    for key in (STATUS_LIKELY_OPEN, STATUS_LIKELY_RELEASED, STATUS_NONE, STATUS_UNKNOWN):
        print(f"  {key}: {counts.get(key, 0)}")
    errors = sum(1 for status in statuses if status.search_error)
    if errors:
        print(f"  search errors: {errors}")
    print(
        "  note: mortgage_status is heuristic; compare raw documents before drawing conclusions"
    )


def fetch_documents(
    units: list[ParcelUnit],
    *,
    username: str,
    password: str,
    headless: bool,
    delay: tuple[float, float],
    resume: bool,
    cache_file: Path,
) -> dict[str, list[BocDocument]]:
    cache = load_cache(cache_file) if resume else {}
    pending = pending_units(units, cache, resume=resume)
    if not pending:
        return {
            unit.boc_tmk: documents_from_cache(cache.get(unit.boc_tmk, []), unit.boc_tmk)
            for unit in units
        }

    scraper = BocScraper(
        username=username,
        password=password,
        headless=headless,
        request_delay=delay,
    )
    driver = scraper._create_driver()
    try:
        scraper.login(driver)
        for index, unit in enumerate(pending, start=1):
            logger.info(
                "BOC search %d/%d: %s unit %s",
                index,
                len(pending),
                unit.condominium_name,
                unit.boc_unit,
            )
            names = condominium_names_to_try(unit)
            try:
                documents, _matched_name = search_unit_documents(scraper, driver, unit)
            except Exception as exc:  # noqa: BLE001 - continue other units
                clear_unit_cache(cache, unit.boc_tmk)
                logger.error(
                    "BOC search failed for %s unit %s (%s): %s; not caching",
                    unit.boc_tmk,
                    unit.boc_unit,
                    ", ".join(names),
                    exc,
                )
                save_cache(cache_file, cache)
                continue

            if store_unit_documents(cache, unit.boc_tmk, documents):
                logger.debug(
                    "Cached %d BOC documents for %s unit %s",
                    len(documents),
                    unit.boc_tmk,
                    unit.boc_unit,
                )
            else:
                logger.error(
                    "BOC search returned no documents for %s unit %s (%s); not caching",
                    unit.boc_tmk,
                    unit.boc_unit,
                    ", ".join(names),
                )
            save_cache(cache_file, cache)
    finally:
        driver.quit()

    results: dict[str, list[BocDocument]] = {}
    for unit in units:
        rows = cache.get(unit.boc_tmk, [])
        results[unit.boc_tmk] = documents_from_cache(rows, unit.boc_tmk)
    return results


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    units = load_parcel_units(args.tmks, args.data_root)
    if args.limit is not None:
        units = units[: args.limit]

    prefix = args.tmks.stem
    output_dir = args.output_dir
    units_path = output_dir / f"{prefix}-units.csv"
    write_csv(units_path, UNIT_FIELDS, unit_rows(units))
    logger.info("Wrote %s (%d units)", units_path, len(units))

    if args.dry_run:
        print(f"Dry run: indexed {len(units)} units; no BOC network requests made")
        return 0

    if not args.username or not args.password:
        raise SystemExit("BOC credentials required: pass --username/--password or set BOC_USERNAME/BOC_PASSWORD")

    cache_file = cache_path(output_dir, prefix)
    documents_by_tmk = fetch_documents(
        units,
        username=args.username,
        password=args.password,
        headless=args.headless,
        delay=(args.delay[0], args.delay[1]),
        resume=args.resume,
        cache_file=cache_file,
    )

    cache = load_cache(cache_file)
    document_rows_all: list[dict[str, str]] = []
    statuses: list[MortgageStatus] = []
    for unit in units:
        documents = documents_by_tmk.get(unit.boc_tmk, [])
        error_rows = cache.get(cache_error_key(unit.boc_tmk), [])
        search_error = error_rows[0].get("search_error", "") if error_rows else ""
        status = classify_mortgage_status(unit, documents, search_error=search_error)
        statuses.append(status)
        document_rows_all.extend(document_rows(unit, documents, status.scraped_at))

    documents_path = output_dir / f"{prefix}-documents.csv"
    status_path = output_dir / f"{prefix}-mortgage-status.csv"
    summary_path = output_dir / f"{prefix}-mortgage-summary.csv"
    write_csv(documents_path, DOCUMENT_FIELDS, document_rows_all)
    write_csv(status_path, STATUS_FIELDS, [status_row(status) for status in statuses])
    write_csv(summary_path, SUMMARY_FIELDS, summary_rows(statuses))

    logger.info("Wrote %s", documents_path)
    logger.info("Wrote %s", status_path)
    logger.info("Wrote %s", summary_path)
    print_summary(statuses)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
