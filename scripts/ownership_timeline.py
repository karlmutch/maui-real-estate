#!/usr/bin/env python3
"""Aggregate non-Hawaii owner residency percentages across selected TMK portfolios.

Uses fullownr mailing addresses from the extract year (EXTRACT_YEAR) to classify each
unit's current owners as Hawaii, non-Hawaii, or unknown. After a unit's first recorded
transfer, that snapshot residency is applied retroactively as a proxy for all later
dates — historical owner mailing addresses are not available in the sales extract.

Also reports property tax rate class mix (official Maui County class labels from fullasmt)
and homestead (homeowner) exemption usage across all selected TMKs combined at the end
of each run.

TMKs are excluded from collective rollups until that TMK has at least one transfer
record on or before the snapshot date. Annual rollups show year-end proxy residency
mix plus resolution progress (units entering the resolved pool). Transfer counts
report total sales events and first-time unit resolutions only.
"""

from __future__ import annotations

import argparse
import csv
import logging
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from county_metadata import (
    OwnerAddressEntry,
    OwnerResidencyResult,
    Residency,
    ResidencyConfidence,
    classify_owner_residency_detail,
    effective_owner_mailing_fields,
    is_master_parid,
    is_long_term_rental_tax_class,
    is_owner_occupied_tax_class,
    is_ownership_transfer,
    load_ownraddr_supplement_for_tmk,
    normalize_event_date,
    normalize_owner_name,
    normalize_tax_rate_class,
    parse_exemption_amount,
    residency_shares,
    resolve_tmk_owner_residency_details,
    tax_rate_class_label,
    address_region_shares,
    classify_owner_address_region,
    tmk_key_to_parid,
)

logger = logging.getLogger(__name__)

MASTER_CPR = "0000"
SCOPE_COLLECTIVE = "collective"
EXTRACT_YEAR = 2026
ANNUAL_START_YEAR = 2001

SOURCE_UNRESOLVED = "unresolved"
SOURCE_FULLOWNR_PROXY = "fullownr_proxy"
SOURCE_FULLOWNR_SNAPSHOT = "fullownr_snapshot"

RESIDENCY_PROXY_DISCLAIMER = (
    f"Historical HI/non-HI mix uses {EXTRACT_YEAR} fullownr mailing addresses applied "
    "after each unit's first transfer; flat annual lines reflect a static proxy, not "
    "stable ownership residency over time."
)

PROXY_RESIDENCY_NOTE = (
    f"residency from {EXTRACT_YEAR} fullownr proxy applied after first transfer; "
    "historical owner addresses not in sales extract"
)
BEFORE_FIRST_TRANSFER_NOTE = "before first recorded transfer; residency unresolved"
CURRENT_OWNERSHIP_NOTE = f"current ownership from {EXTRACT_YEAR} fullownr extract"

_AGENT_ADDRESS_MARKERS = (
    "REAL ESTATE SERVICES",
    "VACATION RENTAL",
    "PROPERTY MANAGEMENT",
    " IRA INC",
    "CUSTODIAN",
)

TIMELINE_COLUMNS = [
    "scope",
    "tmk",
    "period_start",
    "period_end",
    "total_units",
    "hi_pct",
    "non_hi_pct",
    "unknown_pct",
    "unresolved_units",
    "unresolved_pct",
    "proxy_resolved_units",
    "proxy_resolved_pct",
    "hi_units",
    "non_hi_units",
    "mixed_units",
    "unknown_units",
    "source",
    "notes",
]

SUMMARY_COLUMNS = [
    "scope",
    "tmk",
    "total_units",
    "hi_pct",
    "non_hi_pct",
    "unknown_pct",
    "hi_pct_excl_flagged_entities",
    "proxy_resolved_units",
    "proxy_resolved_pct",
    "hi_units",
    "non_hi_units",
    "mixed_units",
    "unknown_units",
]

ANNUAL_COLUMNS = [
    "year",
    "scope",
    "tmk",
    "as_of_date",
    "total_units",
    "hi_pct",
    "non_hi_pct",
    "unknown_pct",
    "non_hi_pct_of_resolved",
    "unresolved_units",
    "unresolved_pct",
    "proxy_resolved_units",
    "proxy_resolved_pct",
    "hi_units",
    "non_hi_units",
    "mixed_units",
    "unknown_units",
    "resolved_units",
    "transfer_count",
    "first_transfer_count",
    "newly_resolved_units",
    "tmk_unit_pct",
    "source",
    "notes",
]

UNIT_DETAIL_COLUMNS = [
    "tmk",
    "cpr",
    "unit",
    "parid",
    "owner_name",
    "owner_share_pct",
    "residency",
    "residency_confidence",
    "owner_kind",
    "possible_agent_address",
    "first_transfer_date",
    "mailing_state",
    "mailing_city_state_zip",
    "country",
]

UNKNOWN_RESIDENCY_COLUMNS = [
    "tmk",
    "cpr",
    "unit",
    "parid",
    "owner_name",
    "owner_share_pct",
    "mailing_state",
    "mailing_city_state_zip",
    "country",
    "mailing_address",
    "first_transfer_date",
]

MIN_ARMS_LENGTH_PRICE = 10_000
BILL9_PRE_PERIOD_START = date(2019, 1, 1)
BILL9_PRE_PERIOD_END = date(2024, 5, 1)
BILL9_POST_PERIOD_START = date(2024, 6, 1)
PERIOD_PRE_BILL9 = "pre_bill9"
PERIOD_POST_BILL9 = "post_bill9"
PERIOD_PRE_LABEL = "Pre–Bill 9 (2019-01-01 through 2024-05-01)"
PERIOD_POST_LABEL = "Post–Bill 9 (2024-06-01 through today)"

BILL9_PERIOD_SUMMARY_COLUMNS = [
    "scope",
    "tmk",
    "period",
    "period_label",
    "period_start",
    "period_end",
    "transfer_count",
    "priced_sale_count",
    "transfers_per_year",
    "median_sale_price",
    "mean_sale_price",
    "hi_pct_transfers",
    "non_hi_pct_transfers",
    "unknown_pct_transfers",
    "unique_units_transferred",
    "newly_resolved_units",
    "newly_resolved_hi_pct",
    "newly_resolved_non_hi_pct",
    "portfolio_hi_pct",
    "portfolio_non_hi_pct",
    "portfolio_unknown_pct",
    "non_hi_pct_high_price_transfers",
    "non_hi_pct_low_price_transfers",
    "high_price_transfer_count",
    "low_price_transfer_count",
    "notes",
]

BILL9_PERIOD_COMPARISON_COLUMNS = [
    "scope",
    "tmk",
    "metric",
    "metric_label",
    "pre_period_value",
    "post_period_value",
    "delta",
    "pct_change",
    "notes",
]

BILL9_PRICE_RESIDENCY_COLUMNS = [
    "scope",
    "tmk",
    "period",
    "building_value_bucket",
    "transfer_count",
    "priced_sale_count",
    "median_sale_price",
    "hi_pct_transfers",
    "non_hi_pct_transfers",
    "unknown_pct_transfers",
    "units_in_bucket",
]

BILL9_DISCLAIMER = (
    "Bill 9 period comparison uses pre (2019-01-01 through 2024-05-01) vs post "
    "(2024-06-01 through today). Residency uses "
    f"{EXTRACT_YEAR} fullownr mailing addresses as a proxy for current owners — not historical "
    "buyer residency at each sale. Off-island = non-Hawaii mailing address; on-island = Hawaii. "
    "Price metrics use arm's-length fee conveyances above $10,000. Comparison does not prove "
    "causation between prices and ownership mix."
)

TAX_RATE_CLASS_SUMMARY_COLUMNS = [
    "scope",
    "tmks",
    "tax_rate_class_code",
    "tax_rate_class_label",
    "unit_count",
    "unit_pct",
    "exemption_units",
    "exemption_pct_of_class",
    "hi_units",
    "hi_pct_of_class",
    "usa_units",
    "usa_pct_of_class",
    "foreign_units",
    "foreign_pct_of_class",
    "unknown_units",
    "unknown_pct_of_class",
]

TAX_RATE_CLASS_UNIT_COLUMNS = [
    "tmk",
    "cpr",
    "unit",
    "parid",
    "tax_rate_class_code",
    "tax_rate_class_label",
    "land_class_code",
    "land_exemption",
    "building_exemption",
    "has_any_exemption",
    "is_owner_occupied",
    "is_homestead_exemption",
    "is_ltr_exemption",
    "owner_address_region",
]

HOMESTEAD_EXEMPTION_SUMMARY_COLUMNS = [
    "scope",
    "tmks",
    "total_units",
    "homestead_units",
    "homestead_pct",
    "homestead_hi_units",
    "homestead_hi_pct",
    "homestead_usa_units",
    "homestead_usa_pct",
    "homestead_foreign_units",
    "homestead_foreign_pct",
    "homestead_unknown_units",
    "homestead_unknown_pct",
    "ltr_exemption_units",
    "ltr_exemption_pct",
    "no_exemption_units",
    "no_exemption_pct",
]

TAX_RATE_CLASS_DISCLAIMER = (
    f"Tax rate class from {EXTRACT_YEAR} fullasmt extract (ASMT OVRCLASS / TAX RATE CLASS). "
    "Labels follow Maui County real property tax certification class descriptions. "
    f"Exemptions use LAND EXEMPTION and BUILDING EXEMPTION dollar amounts; tax class 9 "
    "(OWNER-OCCUPIED) is the county homeowner-exemption billing class. "
    f"Owner address regions use {EXTRACT_YEAR} fullownr mailing addresses (HI, other USA, foreign)."
)

HOMESTEAD_EXEMPTION_DISCLAIMER = (
    "Homestead exemption is proxied by tax class 9 (OWNER-OCCUPIED) with a land or building "
    "exemption in the county assessment extract. This reflects county tax billing, not verified "
    "occupancy. Long-term rental exemptions (tax class 12) are reported separately."
)

_ADDRESS_REGION_LABELS = {
    "hi": "HI",
    "usa": "USA",
    "foreign": "foreign",
    "unknown": "unknown",
}


@dataclass(frozen=True)
class UnitRecord:
    tmk: str
    cpr: str
    unit: str
    parid: str


@dataclass(frozen=True)
class OwnerRecord:
    name: str
    residency: Residency
    confidence: ResidencyConfidence
    owner_kind: str
    possible_agent_address: bool
    mailing_state: str
    mailing_city_state_zip: str
    country: str


@dataclass(frozen=True)
class UnitResidency:
    tmk: str
    cpr: str
    unit: str
    parid: str
    owners: tuple[OwnerRecord, ...]
    hi_pct: float
    non_hi_pct: float
    unknown_pct: float
    hi_pct_excl_flagged: float
    classification: str
    first_transfer_date: str


@dataclass
class ResidencyTotals:
    total_units: int = 0
    hi_weight: float = 0.0
    non_hi_weight: float = 0.0
    unknown_weight: float = 0.0
    hi_weight_excl_flagged: float = 0.0
    hi_units: int = 0
    non_hi_units: int = 0
    mixed_units: int = 0
    unknown_units: int = 0

    def to_percentages(self) -> tuple[float, float, float]:
        if self.total_units == 0:
            return 0.0, 0.0, 0.0
        return (
            round(self.hi_weight / self.total_units, 4),
            round(self.non_hi_weight / self.total_units, 4),
            round(self.unknown_weight / self.total_units, 4),
        )

    def hi_pct_excl_flagged(self) -> float:
        if self.total_units == 0:
            return 0.0
        return round(self.hi_weight_excl_flagged / self.total_units, 4)


@dataclass
class ResolutionTotals:
    total_units: int = 0
    unresolved_units: int = 0
    proxy_resolved_units: int = 0

    def unresolved_pct(self) -> float:
        if self.total_units == 0:
            return 0.0
        return round(100.0 * self.unresolved_units / self.total_units, 4)

    def proxy_resolved_pct(self) -> float:
        if self.total_units == 0:
            return 0.0
        return round(100.0 * self.proxy_resolved_units / self.total_units, 4)


def load_tmks(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"TMK file not found: {path}")
    keys = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    if not keys:
        raise ValueError(f"no TMK keys found in {path}")
    return keys


def discover_tmk_file(
    data_root: Path,
    pattern: str,
    tmk: str,
    output_prefix: str,
) -> Path | None:
    patterns = [f"{output_prefix}-{pattern}", pattern]
    for subdir in sorted(data_root.iterdir()):
        if not subdir.is_dir():
            continue
        for candidate_pattern in patterns:
            candidate = subdir / candidate_pattern.format(tmk=tmk)
            if candidate.is_file():
                return candidate
    return None


def prefixed_output_name(output_prefix: str, name: str) -> str:
    return f"{output_prefix}-{name}"


def read_csv_dicts(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8", errors="replace") as handle:
        return list(csv.DictReader(handle))


def normalize_cpr(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits.zfill(4)[-4:]


def tmk_columns(row: dict[str, str]) -> tuple[str, str, str, str, str, str]:
    division = row.get("DIVISION - TMK") or row.get("DIVISION-TMK") or ""
    zone = row.get("ZONE - TMK", "")
    section = row.get("SECTION - TMK", "")
    plat = row.get("PLAT - TMK", "")
    parcel = row.get("PARCEL - TMK", "")
    cpr = normalize_cpr(row.get("CPR - TMK", ""))
    return division.strip(), zone.strip(), section.strip(), plat.strip(), parcel.strip(), cpr


def build_unit_index(pardat_path: Path, tmk: str) -> dict[str, UnitRecord]:
    units: dict[str, UnitRecord] = {}
    for row in read_csv_dicts(pardat_path):
        _, _, _, _, _, cpr = tmk_columns(row)
        if cpr == MASTER_CPR:
            continue
        units[cpr] = UnitRecord(
            tmk=tmk,
            cpr=cpr,
            unit=row.get("UNIT", "").strip(),
            parid=tmk_key_to_parid(tmk, cpr),
        )
    return units


def load_owner_rows(ownr_path: Path) -> dict[str, list[dict[str, str]]]:
    owners_by_cpr: dict[str, list[dict[str, str]]] = {}
    for row in read_csv_dicts(ownr_path):
        _, _, _, _, _, cpr = tmk_columns(row)
        if cpr == MASTER_CPR:
            continue
        name = row.get("OWNER", "").strip()
        if not name:
            continue
        owners_by_cpr.setdefault(cpr, []).append(row)
    return owners_by_cpr


def classify_owner_kind(name: str) -> str:
    upper = name.upper()
    if " TRUST" in upper or upper.endswith(" TR") or " TR," in upper:
        return "trust"
    if " IRA " in upper or upper.endswith(" IRA INC") or upper.endswith(" IRA"):
        return "ira_custodian"
    if any(token in upper for token in (" LLC", " INC", " CORP", " LTD", " LP")):
        return "llc"
    return "individual"


def is_possible_agent_address(name: str, residency: Residency) -> bool:
    if residency != "hi":
        return False
    upper = name.upper()
    return any(marker in upper for marker in _AGENT_ADDRESS_MARKERS)


def owner_residencies_for_shares(
    owners: tuple[OwnerRecord, ...],
    *,
    exclude_flagged_entities: bool = False,
) -> list[Residency]:
    residencies: list[Residency] = []
    for owner in owners:
        residency = owner.residency
        if exclude_flagged_entities and owner.possible_agent_address:
            residency = "unknown"
        residencies.append(residency)
    return residencies


def unit_shares_from_owners(
    owners: tuple[OwnerRecord, ...],
    *,
    exclude_flagged_entities: bool = False,
) -> tuple[float, float, float]:
    residencies = owner_residencies_for_shares(
        owners,
        exclude_flagged_entities=exclude_flagged_entities,
    )
    shares = residency_shares(residencies)
    return round(shares["hi"], 4), round(shares["non_hi"], 4), round(shares["unknown"], 4)


def discover_ownraddr_file(data_root: Path) -> Path | None:
    candidate = data_root / "ownership-data" / "ownraddr.txt"
    return candidate if candidate.is_file() else None


def build_tmk_residency_map(
    owners_by_cpr: dict[str, list[dict[str, str]]],
    ownraddr_supplement: dict[tuple[str, str], dict[str, str]],
) -> dict[tuple[str, str], OwnerResidencyResult]:
    entries: list[OwnerAddressEntry] = []
    for cpr, owner_rows in owners_by_cpr.items():
        for row in owner_rows:
            owner_name = row.get("OWNER", "").strip()
            if not owner_name:
                continue
            mailing_state, mailing_city_state_zip, country = effective_owner_mailing_fields(
                cpr,
                owner_name,
                row,
                ownraddr_supplement,
            )
            entries.append(
                OwnerAddressEntry(
                    cpr=cpr,
                    owner_name=owner_name,
                    mailing_state=mailing_state,
                    mailing_city_state_zip=mailing_city_state_zip,
                    country=country,
                    result=classify_owner_residency_detail(
                        mailing_state,
                        mailing_city_state_zip,
                        country,
                    ),
                )
            )
    return resolve_tmk_owner_residency_details(entries)


def build_unit_residency(
    unit: UnitRecord,
    owner_rows: list[dict[str, str]],
    *,
    residency_map: dict[tuple[str, str], OwnerResidencyResult],
    ownraddr_supplement: dict[tuple[str, str], dict[str, str]],
    first_transfer_date: str,
) -> UnitResidency:
    owners = tuple(
        _owner_record_from_row(unit.cpr, row, residency_map, ownraddr_supplement)
        for row in owner_rows
    )
    hi_pct, non_hi_pct, unknown_pct = unit_shares_from_owners(owners)
    hi_pct_excl_flagged, _, _ = unit_shares_from_owners(owners, exclude_flagged_entities=True)
    classification = classify_unit_bucket(hi_pct, non_hi_pct, unknown_pct)
    return UnitResidency(
        tmk=unit.tmk,
        cpr=unit.cpr,
        unit=unit.unit,
        parid=unit.parid,
        owners=owners,
        hi_pct=hi_pct,
        non_hi_pct=non_hi_pct,
        unknown_pct=unknown_pct,
        hi_pct_excl_flagged=hi_pct_excl_flagged,
        classification=classification,
        first_transfer_date=first_transfer_date,
    )


def _owner_record_from_row(
    cpr: str,
    row: dict[str, str],
    residency_map: dict[tuple[str, str], OwnerResidencyResult],
    ownraddr_supplement: dict[tuple[str, str], dict[str, str]],
) -> OwnerRecord:
    owner_name = row.get("OWNER", "").strip()
    mailing_state, mailing_city_state_zip, country = effective_owner_mailing_fields(
        cpr,
        owner_name,
        row,
        ownraddr_supplement,
    )
    detail = residency_map.get(
        (cpr, normalize_owner_name(owner_name)),
        classify_owner_residency_detail(mailing_state, mailing_city_state_zip, country),
    )
    return OwnerRecord(
        name=owner_name,
        residency=detail.residency,
        confidence=detail.confidence,
        owner_kind=classify_owner_kind(owner_name),
        possible_agent_address=is_possible_agent_address(owner_name, detail.residency),
        mailing_state=mailing_state,
        mailing_city_state_zip=mailing_city_state_zip,
        country=country,
    )


def classify_unit_bucket(hi_pct: float, non_hi_pct: float, unknown_pct: float) -> str:
    if unknown_pct > 0:
        if hi_pct > 0 and non_hi_pct > 0:
            return "mixed"
        if hi_pct > 0:
            return "mixed"
        if non_hi_pct > 0:
            return "mixed"
        return "unknown"
    if hi_pct > 0 and non_hi_pct > 0:
        return "mixed"
    if hi_pct > 0:
        return "hi"
    return "non_hi"


def load_transfer_event_dates(
    sales_path: Path,
    units: dict[str, UnitRecord],
) -> dict[str, list[str]]:
    events_by_parid: dict[str, list[str]] = {unit.parid: [] for unit in units.values()}
    seen: set[tuple[str, str, str]] = set()

    for row in read_csv_dicts(sales_path):
        parid = row.get("PARID", "").strip()
        if not parid or is_master_parid(parid) or parid not in events_by_parid:
            continue

        instrutype = row.get("INSTRUTYPE", row.get("INSTRTYP", "")).strip()
        doc_type = row.get("DOC_TYPE", "").strip()
        if not is_ownership_transfer(instrutype, doc_type):
            continue

        event_date = normalize_event_date(row.get("SALEDATE", ""), row.get("RECORDDATE", ""))
        if not event_date:
            continue

        dedupe_key = (parid, event_date, row.get("INSTRUNO", "").strip())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        events_by_parid[parid].append(event_date)

    for parid in events_by_parid:
        events_by_parid[parid].sort(key=parse_date)
    return events_by_parid


def parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y/%m/%d").date()


def format_date(value: date) -> str:
    return value.strftime("%Y/%m/%d")


def day_before(value: str) -> str:
    return format_date(parse_date(value) - timedelta(days=1))


def year_end(year: int) -> date:
    return date(year, 12, 31)


def unit_resolution_state(event_dates: list[str], as_of: date) -> str:
    if event_dates:
        if as_of < parse_date(event_dates[0]):
            return "unresolved"
        return "proxy"
    if as_of.year >= EXTRACT_YEAR:
        return "proxy"
    return "unresolved"


def aggregate_resolution_as_of(
    units: list[UnitResidency],
    event_dates_by_parid: dict[str, list[str]],
    as_of: date,
) -> ResolutionTotals:
    totals = ResolutionTotals()
    for unit in units:
        totals.total_units += 1
        state = unit_resolution_state(event_dates_by_parid.get(unit.parid, []), as_of)
        if state == "unresolved":
            totals.unresolved_units += 1
        else:
            totals.proxy_resolved_units += 1
    return totals


def aggregate_resolution_for_period(
    units: list[UnitResidency],
    event_dates_by_parid: dict[str, list[str]],
    period_start: str,
    period_end: str,
) -> ResolutionTotals:
    as_of = as_of_for_period(period_start, period_end)
    return aggregate_resolution_as_of(units, event_dates_by_parid, as_of)


def unit_shares_as_of(
    unit: UnitResidency,
    event_dates: list[str],
    as_of: date,
) -> tuple[float, float, float]:
    """Return residency shares for a unit as of a calendar date."""
    if event_dates:
        if as_of < parse_date(event_dates[0]):
            return 0.0, 0.0, 100.0
        return unit.hi_pct, unit.non_hi_pct, unit.unknown_pct

    if as_of.year < EXTRACT_YEAR:
        return 0.0, 0.0, 100.0
    return unit.hi_pct, unit.non_hi_pct, unit.unknown_pct


def unit_shares_for_period(
    unit: UnitResidency,
    event_dates: list[str],
    period_start: str,
    period_end: str,
) -> tuple[float, float, float]:
    if not period_end:
        as_of = year_end(EXTRACT_YEAR)
        if period_start:
            start = parse_date(period_start)
            as_of = max(as_of, start)
        return unit_shares_as_of(unit, event_dates, as_of)

    period_end_date = parse_date(period_end)
    return unit_shares_as_of(unit, event_dates, period_end_date)


def aggregate_residency(
    units: list[UnitResidency],
    event_dates_by_parid: dict[str, list[str]],
    period_start: str,
    period_end: str,
) -> ResidencyTotals:
    totals = ResidencyTotals()
    for unit in units:
        hi_pct, non_hi_pct, unknown_pct = unit_shares_for_period(
            unit,
            event_dates_by_parid.get(unit.parid, []),
            period_start,
            period_end,
        )
        add_unit_to_totals(totals, unit, hi_pct, non_hi_pct, unknown_pct)
    return totals


def aggregate_residency_as_of(
    units: list[UnitResidency],
    event_dates_by_parid: dict[str, list[str]],
    as_of: date,
) -> ResidencyTotals:
    totals = ResidencyTotals()
    for unit in units:
        hi_pct, non_hi_pct, unknown_pct = unit_shares_as_of(
            unit,
            event_dates_by_parid.get(unit.parid, []),
            as_of,
        )
        add_unit_to_totals(totals, unit, hi_pct, non_hi_pct, unknown_pct)
    return totals


def add_unit_to_totals(
    totals: ResidencyTotals,
    unit: UnitResidency,
    hi_pct: float,
    non_hi_pct: float,
    unknown_pct: float,
) -> None:
    totals.total_units += 1
    totals.hi_weight += hi_pct
    totals.non_hi_weight += non_hi_pct
    totals.unknown_weight += unknown_pct
    totals.hi_weight_excl_flagged += unit.hi_pct_excl_flagged

    if hi_pct > 0 and non_hi_pct > 0:
        totals.mixed_units += 1
    elif unknown_pct > 0:
        totals.unknown_units += 1
    elif hi_pct > 0:
        totals.hi_units += 1
    else:
        totals.non_hi_units += 1


def build_periods(event_dates: list[str]) -> list[tuple[str, str, str]]:
    if not event_dates:
        return [("", "", CURRENT_OWNERSHIP_NOTE)]

    periods: list[tuple[str, str, str]] = [
        ("", day_before(event_dates[0]), BEFORE_FIRST_TRANSFER_NOTE),
    ]
    for index, event_date in enumerate(event_dates[:-1]):
        periods.append(
            (
                event_date,
                day_before(event_dates[index + 1]),
                f"between transfer events; {PROXY_RESIDENCY_NOTE}",
            )
        )
    periods.append((event_dates[-1], "", CURRENT_OWNERSHIP_NOTE))
    return periods


def period_source(period_start: str, period_end: str) -> str:
    if not period_end:
        return SOURCE_FULLOWNR_SNAPSHOT
    if not period_start:
        return SOURCE_UNRESOLVED
    return SOURCE_FULLOWNR_PROXY


def collect_scope_periods(
    units: list[UnitResidency],
    event_dates_by_parid: dict[str, list[str]],
) -> list[tuple[str, str, str]]:
    unique_dates = sorted(
        {
            event_date
            for unit in units
            for event_date in event_dates_by_parid.get(unit.parid, [])
        },
        key=parse_date,
    )
    return build_periods(unique_dates)


def collect_annual_years() -> list[int]:
    return list(range(ANNUAL_START_YEAR, EXTRACT_YEAR + 1))


@dataclass
class YearTransferCounts:
    total: int = 0
    first_transfer: int = 0
    newly_resolved_units: int = 0


def count_year_transfers(
    units: list[UnitResidency],
    event_dates_by_parid: dict[str, list[str]],
    year: int,
) -> YearTransferCounts:
    """Count transfers in a year and units newly entering the resolved pool."""
    counts = YearTransferCounts()
    units_by_parid = {unit.parid: unit for unit in units}
    for parid, dates in event_dates_by_parid.items():
        if units_by_parid.get(parid) is None:
            continue
        for event_date in dates:
            if parse_date(event_date).year != year:
                continue
            counts.total += 1
            if event_date == dates[0]:
                counts.first_transfer += 1
                counts.newly_resolved_units += 1
    return counts


def collective_year_transfers(results: list[TmkResult], year: int) -> YearTransferCounts:
    counts = YearTransferCounts()
    for result in results:
        partial = count_year_transfers(result.units, result.event_dates_by_parid, year)
        counts.total += partial.total
        counts.first_transfer += partial.first_transfer
        counts.newly_resolved_units += partial.newly_resolved_units
    return counts


def tmk_first_record_date(result: TmkResult) -> date | None:
    earliest: date | None = None
    for dates in result.event_dates_by_parid.values():
        if not dates:
            continue
        first = parse_date(dates[0])
        if earliest is None or first < earliest:
            earliest = first
    return earliest


def tmk_active_as_of(result: TmkResult, as_of: date) -> bool:
    first_record = tmk_first_record_date(result)
    if first_record is None:
        return False
    return as_of >= first_record


def as_of_for_period(period_start: str, period_end: str) -> date:
    if period_end:
        return parse_date(period_end)
    if period_start:
        return parse_date(period_start)
    return year_end(EXTRACT_YEAR)


def active_collective_scope(
    results: list[TmkResult],
    as_of: date,
) -> tuple[list[UnitResidency], dict[str, list[str]], list[str]]:
    units: list[UnitResidency] = []
    events: dict[str, list[str]] = {}
    active_tmks: list[str] = []
    for result in results:
        if not tmk_active_as_of(result, as_of):
            continue
        active_tmks.append(result.tmk)
        units.extend(result.units)
        events.update(result.event_dates_by_parid)
    return units, events, active_tmks


def empty_residency_totals() -> ResidencyTotals:
    return ResidencyTotals()


def non_hi_pct_of_resolved(totals: ResidencyTotals) -> float:
    resolved_weight = totals.hi_weight + totals.non_hi_weight
    if resolved_weight <= 0:
        return 0.0
    return round(100.0 * totals.non_hi_weight / resolved_weight, 4)


def totals_to_annual_row(
    year: int,
    scope: str,
    tmk: str,
    totals: ResidencyTotals,
    resolution: ResolutionTotals,
    collective_unit_count: int,
    source: str,
    notes: str,
    transfer_counts: YearTransferCounts,
) -> dict[str, str]:
    hi_pct, non_hi_pct, unknown_pct = totals.to_percentages()
    resolved_units = totals.total_units - totals.unknown_units
    tmk_unit_pct = 0.0
    if collective_unit_count > 0:
        tmk_unit_pct = round(100.0 * totals.total_units / collective_unit_count, 4)
    return {
        "year": str(year),
        "scope": scope,
        "tmk": tmk,
        "as_of_date": format_date(year_end(year)),
        "total_units": str(totals.total_units),
        "hi_pct": f"{hi_pct:.4f}",
        "non_hi_pct": f"{non_hi_pct:.4f}",
        "unknown_pct": f"{unknown_pct:.4f}",
        "non_hi_pct_of_resolved": f"{non_hi_pct_of_resolved(totals):.4f}",
        "unresolved_units": str(resolution.unresolved_units),
        "unresolved_pct": f"{resolution.unresolved_pct():.4f}",
        "proxy_resolved_units": str(resolution.proxy_resolved_units),
        "proxy_resolved_pct": f"{resolution.proxy_resolved_pct():.4f}",
        "hi_units": str(totals.hi_units),
        "non_hi_units": str(totals.non_hi_units),
        "mixed_units": str(totals.mixed_units),
        "unknown_units": str(totals.unknown_units),
        "resolved_units": str(resolved_units),
        "transfer_count": str(transfer_counts.total),
        "first_transfer_count": str(transfer_counts.first_transfer),
        "newly_resolved_units": str(transfer_counts.newly_resolved_units),
        "tmk_unit_pct": f"{tmk_unit_pct:.4f}",
        "source": source,
        "notes": notes,
    }


def empty_resolution_totals() -> ResolutionTotals:
    return ResolutionTotals()


def build_annual_rows(
    results: list[TmkResult],
) -> list[dict[str, str]]:
    years = collect_annual_years()
    full_collective_unit_count = sum(len(result.units) for result in results)
    rows: list[dict[str, str]] = []

    for year in years:
        as_of = year_end(year)
        year_transfers = collective_year_transfers(results, year)
        active_units, active_events, active_tmks = active_collective_scope(results, as_of)
        collective_unit_count = len(active_units)

        if not active_units:
            rows.append(
                totals_to_annual_row(
                    year,
                    SCOPE_COLLECTIVE,
                    "",
                    empty_residency_totals(),
                    empty_resolution_totals(),
                    full_collective_unit_count,
                    "inactive",
                    "no TMKs with transfer records yet",
                    year_transfers,
                )
            )
            continue

        collective_totals = aggregate_residency_as_of(active_units, active_events, as_of)
        collective_resolution = aggregate_resolution_as_of(active_units, active_events, as_of)
        resolved = collective_totals.total_units - collective_totals.unknown_units
        source = SOURCE_FULLOWNR_SNAPSHOT if year >= EXTRACT_YEAR else SOURCE_FULLOWNR_PROXY
        active_tmk_note = f"active TMKs: {', '.join(active_tmks)}"
        notes = (
            f"year-end snapshot; {PROXY_RESIDENCY_NOTE}; {active_tmk_note}"
            if resolved
            else f"year-end snapshot; no units with proxy residency; {active_tmk_note}"
        )
        rows.append(
            totals_to_annual_row(
                year,
                SCOPE_COLLECTIVE,
                "",
                collective_totals,
                collective_resolution,
                collective_unit_count,
                source,
                notes,
                year_transfers,
            )
        )

        for result in results:
            if not tmk_active_as_of(result, as_of):
                continue
            tmk_totals = aggregate_residency_as_of(result.units, result.event_dates_by_parid, as_of)
            tmk_resolution = aggregate_resolution_as_of(
                result.units,
                result.event_dates_by_parid,
                as_of,
            )
            rows.append(
                totals_to_annual_row(
                    year,
                    "tmk",
                    result.tmk,
                    tmk_totals,
                    tmk_resolution,
                    collective_unit_count,
                    source,
                    f"TMK {result.tmk} share of active collective portfolio at year-end",
                    count_year_transfers(result.units, result.event_dates_by_parid, year),
                )
            )

    return rows


def merge_event_dates(results: list[TmkResult]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for result in results:
        for parid, dates in result.event_dates_by_parid.items():
            merged[parid] = dates
    return merged


def totals_to_timeline_row(
    scope: str,
    tmk: str,
    period_start: str,
    period_end: str,
    totals: ResidencyTotals,
    resolution: ResolutionTotals,
    source: str,
    notes: str,
) -> dict[str, str]:
    hi_pct, non_hi_pct, unknown_pct = totals.to_percentages()
    return {
        "scope": scope,
        "tmk": tmk,
        "period_start": period_start,
        "period_end": period_end,
        "total_units": str(totals.total_units),
        "hi_pct": f"{hi_pct:.4f}",
        "non_hi_pct": f"{non_hi_pct:.4f}",
        "unknown_pct": f"{unknown_pct:.4f}",
        "unresolved_units": str(resolution.unresolved_units),
        "unresolved_pct": f"{resolution.unresolved_pct():.4f}",
        "proxy_resolved_units": str(resolution.proxy_resolved_units),
        "proxy_resolved_pct": f"{resolution.proxy_resolved_pct():.4f}",
        "hi_units": str(totals.hi_units),
        "non_hi_units": str(totals.non_hi_units),
        "mixed_units": str(totals.mixed_units),
        "unknown_units": str(totals.unknown_units),
        "source": source,
        "notes": notes,
    }


def totals_to_summary_row(
    scope: str,
    tmk: str,
    totals: ResidencyTotals,
    resolution: ResolutionTotals,
) -> dict[str, str]:
    hi_pct, non_hi_pct, unknown_pct = totals.to_percentages()
    return {
        "scope": scope,
        "tmk": tmk,
        "total_units": str(totals.total_units),
        "hi_pct": f"{hi_pct:.4f}",
        "non_hi_pct": f"{non_hi_pct:.4f}",
        "unknown_pct": f"{unknown_pct:.4f}",
        "hi_pct_excl_flagged_entities": f"{totals.hi_pct_excl_flagged():.4f}",
        "proxy_resolved_units": str(resolution.proxy_resolved_units),
        "proxy_resolved_pct": f"{resolution.proxy_resolved_pct():.4f}",
        "hi_units": str(totals.hi_units),
        "non_hi_units": str(totals.non_hi_units),
        "mixed_units": str(totals.mixed_units),
        "unknown_units": str(totals.unknown_units),
    }


def unit_detail_rows(unit: UnitResidency) -> list[dict[str, str]]:
    if not unit.owners:
        share = 100.0
        return [
            {
                "tmk": unit.tmk,
                "cpr": unit.cpr,
                "unit": unit.unit,
                "parid": unit.parid,
                "owner_name": "",
                "owner_share_pct": f"{share:.4f}",
                "residency": "unknown",
                "residency_confidence": "unknown",
                "owner_kind": "",
                "possible_agent_address": "false",
                "first_transfer_date": unit.first_transfer_date,
                "mailing_state": "",
                "mailing_city_state_zip": "",
                "country": "",
            }
        ]

    share = round(100.0 / len(unit.owners), 4)
    return [
        {
            "tmk": unit.tmk,
            "cpr": unit.cpr,
            "unit": unit.unit,
            "parid": unit.parid,
            "owner_name": owner.name,
            "owner_share_pct": f"{share:.4f}",
            "residency": owner.residency,
            "residency_confidence": owner.confidence,
            "owner_kind": owner.owner_kind,
            "possible_agent_address": "true" if owner.possible_agent_address else "false",
            "first_transfer_date": unit.first_transfer_date,
            "mailing_state": owner.mailing_state,
            "mailing_city_state_zip": owner.mailing_city_state_zip,
            "country": owner.country,
        }
        for owner in unit.owners
    ]


def format_mailing_address(
    mailing_state: str,
    mailing_city_state_zip: str,
    country: str,
) -> str:
    parts = [part for part in (mailing_state.strip(), mailing_city_state_zip.strip(), country.strip()) if part]
    return ", ".join(parts) if parts else "(no mailing address on file)"


def collect_unknown_residency_rows(results: list[TmkResult]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for result in results:
        for detail in result.unit_detail_rows:
            if detail["residency"] != "unknown":
                continue
            rows.append(
                {
                    "tmk": detail["tmk"],
                    "cpr": detail["cpr"],
                    "unit": detail["unit"],
                    "parid": detail["parid"],
                    "owner_name": detail["owner_name"],
                    "owner_share_pct": detail["owner_share_pct"],
                    "mailing_state": detail["mailing_state"],
                    "mailing_city_state_zip": detail["mailing_city_state_zip"],
                    "country": detail["country"],
                    "mailing_address": format_mailing_address(
                        detail["mailing_state"],
                        detail["mailing_city_state_zip"],
                        detail["country"],
                    ),
                    "first_transfer_date": detail["first_transfer_date"],
                }
            )
    rows.sort(key=lambda row: (row["tmk"], row["unit"], row["cpr"], row["owner_name"]))
    return rows


def print_unknown_residency_addresses(rows: list[dict[str, str]]) -> None:
    print(f"Unknown residency owners ({len(rows)} owner row(s), current snapshot)")
    if not rows:
        print("  (none)")
        print()
        return

    current_tmk = ""
    for row in rows:
        if row["tmk"] != current_tmk:
            current_tmk = row["tmk"]
            print(f"  TMK {current_tmk}")
        owner_label = row["owner_name"] or "(no owner name)"
        unit_label = row["unit"] or row["cpr"]
        print(
            f"    {unit_label} (CPR {row['cpr']}, {row['owner_share_pct']}%) "
            f"— {owner_label} — {row['mailing_address']}"
        )
    print()


def write_csv(path: Path, columns: list[str], rows: list[dict[str, str]], dry_run: bool) -> None:
    if dry_run:
        logger.info("would write %s (%d rows)", path, len(rows))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


@dataclass
class TmkResult:
    tmk: str
    units: list[UnitResidency]
    event_dates_by_parid: dict[str, list[str]]
    timeline_rows: list[dict[str, str]]
    summary_row: dict[str, str]
    unit_detail_rows: list[dict[str, str]]


def process_tmk(
    tmk: str,
    data_root: Path,
    output_dir: Path,
    output_prefix: str,
    dry_run: bool,
) -> TmkResult:
    pardat_path = discover_tmk_file(data_root, "fullpardat26-{tmk}.txt", tmk, output_prefix)
    ownr_path = discover_tmk_file(data_root, "fullownr26-{tmk}.txt", tmk, output_prefix)
    sales_path = discover_tmk_file(data_root, "sales-{tmk}.csv", tmk, output_prefix)

    missing = [
        label
        for label, path in (
            ("fullpardat", pardat_path),
            ("fullownr", ownr_path),
            ("sales", sales_path),
        )
        if path is None
    ]
    if missing:
        raise FileNotFoundError(f"missing required input(s) for TMK {tmk}: {', '.join(missing)}")

    units_index = build_unit_index(pardat_path, tmk)
    owners_by_cpr = load_owner_rows(ownr_path)
    event_dates_by_parid = load_transfer_event_dates(sales_path, units_index)
    ownraddr_path = discover_ownraddr_file(data_root)
    ownraddr_supplement = (
        load_ownraddr_supplement_for_tmk(ownraddr_path, tmk)
        if ownraddr_path is not None
        else {}
    )
    residency_map = build_tmk_residency_map(owners_by_cpr, ownraddr_supplement)

    units = []
    for cpr in sorted(units_index):
        event_dates = event_dates_by_parid.get(units_index[cpr].parid, [])
        first_transfer_date = event_dates[0] if event_dates else ""
        units.append(
            build_unit_residency(
                units_index[cpr],
                owners_by_cpr.get(cpr, []),
                residency_map=residency_map,
                ownraddr_supplement=ownraddr_supplement,
                first_transfer_date=first_transfer_date,
            )
        )

    timeline_rows: list[dict[str, str]] = []
    for period_start, period_end, notes in collect_scope_periods(units, event_dates_by_parid):
        totals = aggregate_residency(units, event_dates_by_parid, period_start, period_end)
        resolution = aggregate_resolution_for_period(
            units,
            event_dates_by_parid,
            period_start,
            period_end,
        )
        source = period_source(period_start, period_end)
        timeline_rows.append(
            totals_to_timeline_row(
                "tmk",
                tmk,
                period_start,
                period_end,
                totals,
                resolution,
                source,
                notes,
            )
        )

    current_totals = aggregate_residency(units, event_dates_by_parid, "", "")
    current_resolution = aggregate_resolution_as_of(
        units,
        event_dates_by_parid,
        year_end(EXTRACT_YEAR),
    )
    summary_row = totals_to_summary_row("tmk", tmk, current_totals, current_resolution)
    detail_rows: list[dict[str, str]] = []
    for unit in units:
        detail_rows.extend(unit_detail_rows(unit))

    unit_detail_path = output_dir / prefixed_output_name(
        output_prefix, f"non-hi-ownership-units-{tmk}.csv"
    )
    write_csv(unit_detail_path, UNIT_DETAIL_COLUMNS, detail_rows, dry_run)

    return TmkResult(
        tmk=tmk,
        units=units,
        event_dates_by_parid=event_dates_by_parid,
        timeline_rows=timeline_rows,
        summary_row=summary_row,
        unit_detail_rows=detail_rows,
    )


def build_collective_rows(results: list[TmkResult]) -> tuple[list[dict[str, str]], dict[str, str]]:
    all_units = [unit for result in results for unit in result.units]
    all_events = merge_event_dates(results)

    timeline_rows: list[dict[str, str]] = []
    for period_start, period_end, notes in collect_scope_periods(all_units, all_events):
        as_of = as_of_for_period(period_start, period_end)
        active_units, active_events, active_tmks = active_collective_scope(results, as_of)
        if not active_units:
            continue

        totals = aggregate_residency(active_units, active_events, period_start, period_end)
        resolution = aggregate_resolution_for_period(
            active_units,
            active_events,
            period_start,
            period_end,
        )
        source = period_source(period_start, period_end)
        active_tmk_note = f"active TMKs: {', '.join(active_tmks)}"
        timeline_rows.append(
            totals_to_timeline_row(
                SCOPE_COLLECTIVE,
                "",
                period_start,
                period_end,
                totals,
                resolution,
                source,
                f"{notes}; {active_tmk_note}",
            )
        )

    current_units, current_events, _ = active_collective_scope(results, year_end(EXTRACT_YEAR))
    current_totals = aggregate_residency(current_units, current_events, "", "")
    current_resolution = aggregate_resolution_as_of(
        current_units,
        current_events,
        year_end(EXTRACT_YEAR),
    )
    summary_row = totals_to_summary_row(SCOPE_COLLECTIVE, "", current_totals, current_resolution)
    return timeline_rows, summary_row


def print_summary(
    results: list[TmkResult],
    collective_summary: dict[str, str],
    timeline_path: Path,
    summary_path: Path,
    annual_path: Path,
    unknown_residency_path: Path,
    annual_rows: list[dict[str, str]],
    unknown_residency_rows: list[dict[str, str]],
    dry_run: bool,
) -> None:
    action = "Would write" if dry_run else "Wrote"
    print(f"\n{action} residency timeline: {timeline_path}")
    print(f"{action} residency summary: {summary_path}")
    print(f"{action} annual residency: {annual_path}")
    print(f"{action} unknown residency addresses: {unknown_residency_path}")
    print()
    print(RESIDENCY_PROXY_DISCLAIMER)
    print()
    print("Collective portfolio (current snapshot)")
    print(f"  total units: {collective_summary['total_units']}")
    print(f"  Hawaii residency (proxy): {collective_summary['hi_pct']}%")
    print(
        "  Hawaii residency excl. flagged entities: "
        f"{collective_summary['hi_pct_excl_flagged_entities']}%"
    )
    print(f"  non-Hawaii residency (proxy): {collective_summary['non_hi_pct']}%")
    print(f"  unknown residency: {collective_summary['unknown_pct']}%")
    print(
        f"  proxy resolved units: {collective_summary['proxy_resolved_units']} "
        f"({collective_summary['proxy_resolved_pct']}%)"
    )
    print(f"  HI units: {collective_summary['hi_units']}")
    print(f"  non-HI units: {collective_summary['non_hi_units']}")
    print(f"  mixed units: {collective_summary['mixed_units']}")
    print(f"  unknown units: {collective_summary['unknown_units']}")
    print()
    print_unknown_residency_addresses(unknown_residency_rows)

    collective_annual = [
        row for row in annual_rows if row["scope"] == SCOPE_COLLECTIVE and row["year"].isdigit()
    ]
    if collective_annual:
        print(
            f"Collective annual proxy residency "
            f"(year-end, {ANNUAL_START_YEAR}–{EXTRACT_YEAR})"
        )
        for row in collective_annual:
            print(
                f"  {row['year']}: {row['non_hi_pct']}% non-HI proxy "
                f"({row['non_hi_pct_of_resolved']}% of resolved), "
                f"{row['unknown_pct']}% unknown, "
                f"proxy resolved: {row['proxy_resolved_units']}/{row['total_units']} "
                f"({row['proxy_resolved_pct']}%), "
                f"newly resolved: {row['newly_resolved_units']}, "
                f"transfers: {row['transfer_count']} "
                f"(first transfer: {row['first_transfer_count']})"
            )
        print()

    for result in results:
        summary = result.summary_row
        print(f"TMK {result.tmk}")
        print(f"  total units: {summary['total_units']}")
        print(f"  non-Hawaii residency (proxy): {summary['non_hi_pct']}%")
        print(f"  Hawaii residency (proxy): {summary['hi_pct']}%")
        print(
            "  Hawaii residency excl. flagged entities: "
            f"{summary['hi_pct_excl_flagged_entities']}%"
        )
        print(f"  unknown residency: {summary['unknown_pct']}%")
        print()


@dataclass(frozen=True)
class TransferSaleEvent:
    tmk: str
    parid: str
    cpr: str
    unit: str
    sale_date: str
    year: int
    price: float | None
    building_value_bucket: str


def parse_sale_price(value: str) -> float | None:
    cleaned = value.strip().replace(",", "")
    if not cleaned:
        return None
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    if amount < MIN_ARMS_LENGTH_PRICE:
        return None
    return amount


def normalize_building_value(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if not digits:
        return ""
    return str(int(digits))


def load_building_values_by_cpr(asmt_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for row in read_csv_dicts(asmt_path):
        _, _, _, _, _, cpr = tmk_columns(row)
        if cpr == MASTER_CPR:
            continue
        bucket = normalize_building_value(row.get("ASSESSED BUILDING VALUE", ""))
        if bucket:
            values[cpr] = bucket
    return values


def load_transfer_sale_events(
    tmk: str,
    sales_path: Path,
    units_index: dict[str, UnitRecord],
    building_values_by_cpr: dict[str, str],
) -> list[TransferSaleEvent]:
    parid_to_unit = {unit.parid: unit for unit in units_index.values()}
    events: list[TransferSaleEvent] = []
    seen: set[tuple[str, str, str]] = set()

    for row in read_csv_dicts(sales_path):
        parid = row.get("PARID", "").strip()
        if not parid or is_master_parid(parid):
            continue
        unit = parid_to_unit.get(parid)
        if unit is None:
            continue

        instrutype = row.get("INSTRUTYPE", row.get("INSTRTYP", "")).strip()
        doc_type = row.get("DOC_TYPE", "").strip()
        if not is_ownership_transfer(instrutype, doc_type):
            continue

        event_date = normalize_event_date(row.get("SALEDATE", ""), row.get("RECORDDATE", ""))
        if not event_date:
            continue

        dedupe_key = (parid, event_date, row.get("INSTRUNO", "").strip())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        bucket = building_values_by_cpr.get(unit.cpr, "")
        events.append(
            TransferSaleEvent(
                tmk=tmk,
                parid=parid,
                cpr=unit.cpr,
                unit=unit.unit,
                sale_date=event_date,
                year=parse_date(event_date).year,
                price=parse_sale_price(row.get("PRICE", "")),
                building_value_bucket=bucket,
            )
        )
    return events


def bucket_unit_counts(building_values_by_cpr: dict[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for bucket in building_values_by_cpr.values():
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def median_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def mean_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.mean(values))


def format_optional_float(value: float | None, *, digits: int = 2) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def format_pct(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.4f}"


def bill9_post_period_end() -> date:
    return date.today()


def bill9_period_date_bounds(period: str) -> tuple[date, date]:
    if period == PERIOD_PRE_BILL9:
        return BILL9_PRE_PERIOD_START, BILL9_PRE_PERIOD_END
    return BILL9_POST_PERIOD_START, bill9_post_period_end()


def sale_date_in_bill9_period(sale_date: date, period: str) -> bool:
    period_start, period_end = bill9_period_date_bounds(period)
    return period_start <= sale_date <= period_end


def period_length_years(period_start: date, period_end: date) -> float:
    return max(1.0 / 365.25, ((period_end - period_start).days + 1) / 365.25)


def weighted_residency_percentages(
    hi_weight: float,
    non_hi_weight: float,
    unknown_weight: float,
) -> tuple[float, float, float]:
    total = hi_weight + non_hi_weight + unknown_weight
    if total <= 0:
        return 0.0, 0.0, 0.0
    return (
        round(100.0 * hi_weight / total, 4),
        round(100.0 * non_hi_weight / total, 4),
        round(100.0 * unknown_weight / total, 4),
    )


@dataclass
class PeriodAnalysis:
    scope: str
    tmk: str
    period: str
    period_label: str
    period_start: date
    period_end: date
    transfer_count: int
    priced_sale_count: int
    transfers_per_year: float
    median_sale_price: float | None
    mean_sale_price: float | None
    hi_pct_transfers: float
    non_hi_pct_transfers: float
    unknown_pct_transfers: float
    unique_units_transferred: int
    newly_resolved_units: int
    newly_resolved_hi_pct: float
    newly_resolved_non_hi_pct: float
    portfolio_hi_pct: float
    portfolio_non_hi_pct: float
    portfolio_unknown_pct: float
    non_hi_pct_high_price_transfers: float | None
    non_hi_pct_low_price_transfers: float | None
    high_price_transfer_count: int
    low_price_transfer_count: int
    notes: str


def cohort_residency_percentages(units: list[UnitResidency]) -> tuple[float, float, float]:
    totals = ResidencyTotals()
    for unit in units:
        add_unit_to_totals(totals, unit, unit.hi_pct, unit.non_hi_pct, unit.unknown_pct)
    return totals.to_percentages()


def compute_period_analysis(
    scope: str,
    tmk: str,
    period: str,
    units: list[UnitResidency],
    events: list[TransferSaleEvent],
    event_dates_by_parid: dict[str, list[str]],
) -> PeriodAnalysis:
    period_start, period_end = bill9_period_date_bounds(period)
    period_events = [
        event
        for event in events
        if sale_date_in_bill9_period(parse_date(event.sale_date), period)
    ]
    units_by_parid = {unit.parid: unit for unit in units}
    period_label = PERIOD_PRE_LABEL if period == PERIOD_PRE_BILL9 else PERIOD_POST_LABEL

    hi_weight = non_hi_weight = unknown_weight = 0.0
    for event in period_events:
        unit = units_by_parid.get(event.parid)
        if unit is None:
            continue
        hi_weight += unit.hi_pct
        non_hi_weight += unit.non_hi_pct
        unknown_weight += unit.unknown_pct

    hi_pct_transfers, non_hi_pct_transfers, unknown_pct_transfers = weighted_residency_percentages(
        hi_weight,
        non_hi_weight,
        unknown_weight,
    )

    priced_prices = [event.price for event in period_events if event.price is not None]
    median_sale_price = median_or_none(priced_prices)
    mean_sale_price = mean_or_none(priced_prices)

    high_hi = high_non_hi = high_unknown = 0.0
    low_hi = low_non_hi = low_unknown = 0.0
    high_price_transfer_count = 0
    low_price_transfer_count = 0
    if median_sale_price is not None:
        for event in period_events:
            if event.price is None:
                continue
            unit = units_by_parid.get(event.parid)
            if unit is None:
                continue
            if event.price >= median_sale_price:
                high_hi += unit.hi_pct
                high_non_hi += unit.non_hi_pct
                high_unknown += unit.unknown_pct
                high_price_transfer_count += 1
            else:
                low_hi += unit.hi_pct
                low_non_hi += unit.non_hi_pct
                low_unknown += unit.unknown_pct
                low_price_transfer_count += 1

    _, non_hi_high, _ = weighted_residency_percentages(high_hi, high_non_hi, high_unknown)
    _, non_hi_low, _ = weighted_residency_percentages(low_hi, low_non_hi, low_unknown)
    non_hi_pct_high = non_hi_high if high_price_transfer_count else None
    non_hi_pct_low = non_hi_low if low_price_transfer_count else None

    newly_resolved = [
        unit
        for unit in units
        if unit.first_transfer_date
        and sale_date_in_bill9_period(parse_date(unit.first_transfer_date), period)
    ]
    newly_hi_pct, newly_non_hi_pct, _ = cohort_residency_percentages(newly_resolved)

    as_of = BILL9_PRE_PERIOD_END if period == PERIOD_PRE_BILL9 else bill9_post_period_end()
    portfolio_totals = aggregate_residency_as_of(units, event_dates_by_parid, as_of)
    portfolio_hi_pct, portfolio_non_hi_pct, portfolio_unknown_pct = portfolio_totals.to_percentages()

    unique_units_transferred = len({event.parid for event in period_events})
    transfers_per_year = (
        round(len(period_events) / period_length_years(period_start, period_end), 4)
        if period_events
        else 0.0
    )

    notes = (
        f"portfolio snapshot at {format_date(as_of)}; "
        f"transfer residency from {EXTRACT_YEAR} fullownr proxy"
    )

    return PeriodAnalysis(
        scope=scope,
        tmk=tmk,
        period=period,
        period_label=period_label,
        period_start=period_start,
        period_end=period_end,
        transfer_count=len(period_events),
        priced_sale_count=len(priced_prices),
        transfers_per_year=transfers_per_year,
        median_sale_price=median_sale_price,
        mean_sale_price=mean_sale_price,
        hi_pct_transfers=hi_pct_transfers,
        non_hi_pct_transfers=non_hi_pct_transfers,
        unknown_pct_transfers=unknown_pct_transfers,
        unique_units_transferred=unique_units_transferred,
        newly_resolved_units=len(newly_resolved),
        newly_resolved_hi_pct=newly_hi_pct,
        newly_resolved_non_hi_pct=newly_non_hi_pct,
        portfolio_hi_pct=portfolio_hi_pct,
        portfolio_non_hi_pct=portfolio_non_hi_pct,
        portfolio_unknown_pct=portfolio_unknown_pct,
        non_hi_pct_high_price_transfers=non_hi_pct_high,
        non_hi_pct_low_price_transfers=non_hi_pct_low,
        high_price_transfer_count=high_price_transfer_count,
        low_price_transfer_count=low_price_transfer_count,
        notes=notes,
    )


def period_analysis_to_row(analysis: PeriodAnalysis) -> dict[str, str]:
    return {
        "scope": analysis.scope,
        "tmk": analysis.tmk,
        "period": analysis.period,
        "period_label": analysis.period_label,
        "period_start": format_date(analysis.period_start),
        "period_end": format_date(analysis.period_end),
        "transfer_count": str(analysis.transfer_count),
        "priced_sale_count": str(analysis.priced_sale_count),
        "transfers_per_year": format_pct(analysis.transfers_per_year),
        "median_sale_price": format_optional_float(analysis.median_sale_price, digits=0),
        "mean_sale_price": format_optional_float(analysis.mean_sale_price, digits=0),
        "hi_pct_transfers": format_pct(analysis.hi_pct_transfers),
        "non_hi_pct_transfers": format_pct(analysis.non_hi_pct_transfers),
        "unknown_pct_transfers": format_pct(analysis.unknown_pct_transfers),
        "unique_units_transferred": str(analysis.unique_units_transferred),
        "newly_resolved_units": str(analysis.newly_resolved_units),
        "newly_resolved_hi_pct": format_pct(analysis.newly_resolved_hi_pct),
        "newly_resolved_non_hi_pct": format_pct(analysis.newly_resolved_non_hi_pct),
        "portfolio_hi_pct": format_pct(analysis.portfolio_hi_pct),
        "portfolio_non_hi_pct": format_pct(analysis.portfolio_non_hi_pct),
        "portfolio_unknown_pct": format_pct(analysis.portfolio_unknown_pct),
        "non_hi_pct_high_price_transfers": format_pct(analysis.non_hi_pct_high_price_transfers),
        "non_hi_pct_low_price_transfers": format_pct(analysis.non_hi_pct_low_price_transfers),
        "high_price_transfer_count": str(analysis.high_price_transfer_count),
        "low_price_transfer_count": str(analysis.low_price_transfer_count),
        "notes": analysis.notes,
    }


def comparison_delta(pre_value: float | None, post_value: float | None) -> tuple[str, str]:
    if pre_value is None or post_value is None:
        return "", ""
    delta = round(post_value - pre_value, 4)
    if pre_value == 0:
        return str(delta), ""
    pct_change = round(100.0 * delta / pre_value, 4)
    return str(delta), str(pct_change)


def comparison_metric_value(metric: str, value: float | None) -> str:
    if value is None:
        return ""
    if metric in ("median_sale_price", "mean_sale_price"):
        return format_optional_float(value, digits=0)
    if metric == "transfer_count":
        return str(int(value))
    return format_optional_float(value, digits=4)


def build_period_comparison_rows(
    scope: str,
    tmk: str,
    pre: PeriodAnalysis,
    post: PeriodAnalysis,
) -> list[dict[str, str]]:
    metrics: list[tuple[str, str, float | None, float | None, str]] = [
        (
            "transfer_count",
            "Ownership transfer events",
            float(pre.transfer_count),
            float(post.transfer_count),
            "Total fee-conveyance transfers in period",
        ),
        (
            "transfers_per_year",
            "Annualized transfer rate",
            pre.transfers_per_year,
            post.transfers_per_year,
            "Transfers per calendar year in period",
        ),
        (
            "median_sale_price",
            "Median arm's-length sale price",
            pre.median_sale_price,
            post.median_sale_price,
            "Among priced sales above $10,000",
        ),
        (
            "mean_sale_price",
            "Mean arm's-length sale price",
            pre.mean_sale_price,
            post.mean_sale_price,
            "Among priced sales above $10,000",
        ),
        (
            "non_hi_pct_transfers",
            "Off-island share among transfers",
            pre.non_hi_pct_transfers,
            post.non_hi_pct_transfers,
            "Weighted by unit owner proxy at transfer time",
        ),
        (
            "hi_pct_transfers",
            "On-island (HI) share among transfers",
            pre.hi_pct_transfers,
            post.hi_pct_transfers,
            "Weighted by unit owner proxy at transfer time",
        ),
        (
            "portfolio_non_hi_pct",
            "Portfolio off-island share at period end",
            pre.portfolio_non_hi_pct,
            post.portfolio_non_hi_pct,
            f"Proxy mix as of {format_date(pre.period_end)} vs {format_date(post.period_end)}",
        ),
        (
            "portfolio_hi_pct",
            "Portfolio on-island share at period end",
            pre.portfolio_hi_pct,
            post.portfolio_hi_pct,
            f"Proxy mix as of {format_date(pre.period_end)} vs {format_date(post.period_end)}",
        ),
        (
            "newly_resolved_non_hi_pct",
            "Off-island share of first-time resolved units",
            pre.newly_resolved_non_hi_pct if pre.newly_resolved_units else None,
            post.newly_resolved_non_hi_pct if post.newly_resolved_units else None,
            "Units whose first transfer fell in the period",
        ),
        (
            "newly_resolved_hi_pct",
            "On-island share of first-time resolved units",
            pre.newly_resolved_hi_pct if pre.newly_resolved_units else None,
            post.newly_resolved_hi_pct if post.newly_resolved_units else None,
            "Units whose first transfer fell in the period",
        ),
        (
            "non_hi_pct_high_price_transfers",
            "Off-island share — sales at/above period median price",
            pre.non_hi_pct_high_price_transfers,
            post.non_hi_pct_high_price_transfers,
            "Tests whether expensive sales skew off-island differently by era",
        ),
        (
            "non_hi_pct_low_price_transfers",
            "Off-island share — sales below period median price",
            pre.non_hi_pct_low_price_transfers,
            post.non_hi_pct_low_price_transfers,
            "Tests whether lower-priced sales skew off-island differently by era",
        ),
    ]

    rows: list[dict[str, str]] = []
    for metric, label, pre_value, post_value, notes in metrics:
        delta, pct_change = comparison_delta(pre_value, post_value)
        rows.append(
            {
                "scope": scope,
                "tmk": tmk,
                "metric": metric,
                "metric_label": label,
                "pre_period_value": comparison_metric_value(metric, pre_value),
                "post_period_value": comparison_metric_value(metric, post_value),
                "delta": delta,
                "pct_change": pct_change,
                "notes": notes,
            }
        )
    return rows


def build_price_residency_rows(
    scope: str,
    tmk: str,
    period: str,
    events: list[TransferSaleEvent],
    units_by_parid: dict[str, UnitResidency],
    bucket_unit_counts_map: dict[str, int],
) -> list[dict[str, str]]:
    period_events = [
        event
        for event in events
        if sale_date_in_bill9_period(parse_date(event.sale_date), period)
    ]
    buckets = sorted(
        {event.building_value_bucket for event in period_events if event.building_value_bucket}
    )
    rows: list[dict[str, str]] = []
    for bucket in buckets:
        bucket_events = [
            event for event in period_events if event.building_value_bucket == bucket
        ]
        hi_weight = non_hi_weight = unknown_weight = 0.0
        for event in bucket_events:
            unit = units_by_parid.get(event.parid)
            if unit is None:
                continue
            hi_weight += unit.hi_pct
            non_hi_weight += unit.non_hi_pct
            unknown_weight += unit.unknown_pct
        hi_pct, non_hi_pct, unknown_pct = weighted_residency_percentages(
            hi_weight,
            non_hi_weight,
            unknown_weight,
        )
        priced_prices = [event.price for event in bucket_events if event.price is not None]
        rows.append(
            {
                "scope": scope,
                "tmk": tmk,
                "period": period,
                "building_value_bucket": bucket,
                "transfer_count": str(len(bucket_events)),
                "priced_sale_count": str(len(priced_prices)),
                "median_sale_price": format_optional_float(median_or_none(priced_prices), digits=0),
                "hi_pct_transfers": format_pct(hi_pct),
                "non_hi_pct_transfers": format_pct(non_hi_pct),
                "unknown_pct_transfers": format_pct(unknown_pct),
                "units_in_bucket": str(bucket_unit_counts_map.get(bucket, 0)),
            }
        )
    return rows


def load_tmk_transfer_events(
    tmk: str,
    data_root: Path,
    output_prefix: str,
) -> tuple[list[TransferSaleEvent], dict[str, int]] | None:
    pardat_path = discover_tmk_file(data_root, "fullpardat26-{tmk}.txt", tmk, output_prefix)
    asmt_path = discover_tmk_file(data_root, "fullasmt26-{tmk}.txt", tmk, output_prefix)
    sales_path = discover_tmk_file(data_root, "sales-{tmk}.csv", tmk, output_prefix)
    if pardat_path is None or asmt_path is None or sales_path is None:
        return None
    units_index = build_unit_index(pardat_path, tmk)
    building_values = load_building_values_by_cpr(asmt_path)
    events = load_transfer_sale_events(tmk, sales_path, units_index, building_values)
    return events, bucket_unit_counts(building_values)


def combined_tmk_key(results: list[TmkResult]) -> str:
    return ",".join(result.tmk for result in results)


def analyze_collective_bill9_periods(
    results: list[TmkResult],
    data_root: Path,
    output_prefix: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]] | None:
    if not results:
        return None

    combined_tmks = combined_tmk_key(results)
    all_units = [unit for result in results for unit in result.units]
    all_events: list[TransferSaleEvent] = []
    all_event_dates: dict[str, list[str]] = {}
    bucket_counts: dict[str, int] = {}
    units_by_parid = {unit.parid: unit for unit in all_units}

    for result in results:
        loaded = load_tmk_transfer_events(result.tmk, data_root, output_prefix)
        if loaded is None:
            continue
        events, counts = loaded
        all_events.extend(events)
        for bucket, count in counts.items():
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + count
        all_event_dates.update(result.event_dates_by_parid)

    if not all_events:
        return None

    pre = compute_period_analysis(
        SCOPE_COLLECTIVE,
        combined_tmks,
        PERIOD_PRE_BILL9,
        all_units,
        all_events,
        all_event_dates,
    )
    post = compute_period_analysis(
        SCOPE_COLLECTIVE,
        combined_tmks,
        PERIOD_POST_BILL9,
        all_units,
        all_events,
        all_event_dates,
    )

    summary_rows = [period_analysis_to_row(pre), period_analysis_to_row(post)]
    comparison_rows = build_period_comparison_rows(SCOPE_COLLECTIVE, combined_tmks, pre, post)
    price_residency_rows = build_price_residency_rows(
        SCOPE_COLLECTIVE,
        combined_tmks,
        PERIOD_PRE_BILL9,
        all_events,
        units_by_parid,
        bucket_counts,
    )
    price_residency_rows.extend(
        build_price_residency_rows(
            SCOPE_COLLECTIVE,
            combined_tmks,
            PERIOD_POST_BILL9,
            all_events,
            units_by_parid,
            bucket_counts,
        )
    )
    return summary_rows, comparison_rows, price_residency_rows


def scope_label(scope: str, tmk: str) -> str:
    if scope == SCOPE_COLLECTIVE:
        if tmk:
            return f"All selected TMKs combined ({tmk.replace(',', ', ')})"
        return "All selected TMKs combined"
    return f"TMK {tmk}"


def print_period_summary(analysis: PeriodAnalysis) -> None:
    print(
        f"  {analysis.period_label} "
        f"({format_date(analysis.period_start)}–{format_date(analysis.period_end)})"
    )
    print(
        f"    transfers: {analysis.transfer_count} "
        f"({analysis.transfers_per_year:.2f}/yr), "
        f"priced sales: {analysis.priced_sale_count}"
    )
    if analysis.median_sale_price is not None:
        print(
            f"    median sale price: ${analysis.median_sale_price:,.0f} "
            f"(mean ${analysis.mean_sale_price:,.0f})"
        )
    print(
        f"    off-island among transfers: {analysis.non_hi_pct_transfers:.2f}% "
        f"(on-island {analysis.hi_pct_transfers:.2f}%, proxy)"
    )
    print(
        f"    portfolio at period end: {analysis.portfolio_non_hi_pct:.2f}% off-island, "
        f"{analysis.portfolio_hi_pct:.2f}% on-island "
        f"({analysis.portfolio_unknown_pct:.2f}% unknown)"
    )
    if analysis.newly_resolved_units:
        print(
            f"    first-time resolved units: {analysis.newly_resolved_units} "
            f"({analysis.newly_resolved_non_hi_pct:.2f}% off-island proxy)"
        )
    if analysis.high_price_transfer_count:
        print(
            f"    at/above median price: {analysis.non_hi_pct_high_price_transfers:.2f}% off-island "
            f"({analysis.high_price_transfer_count} sales)"
        )
    if analysis.low_price_transfer_count:
        print(
            f"    below median price: {analysis.non_hi_pct_low_price_transfers:.2f}% off-island "
            f"({analysis.low_price_transfer_count} sales)"
        )


def print_bill9_period_analysis(
    summary_path: Path,
    comparison_path: Path,
    price_residency_path: Path,
    summary_rows: list[dict[str, str]],
    comparison_rows: list[dict[str, str]],
    dry_run: bool,
) -> None:
    action = "Would write" if dry_run else "Wrote"
    print(f"\n{action} Bill 9 period summary: {summary_path}")
    print(f"{action} Bill 9 period comparison: {comparison_path}")
    print(f"{action} Bill 9 price/residency by bucket: {price_residency_path}")
    print()
    print("Maui Bill 9 era comparison — all selected TMKs combined")
    print(BILL9_DISCLAIMER)
    print()

    collective_summaries = [row for row in summary_rows if row["scope"] == SCOPE_COLLECTIVE]
    collective_comparisons = [row for row in comparison_rows if row["scope"] == SCOPE_COLLECTIVE]
    if not collective_summaries:
        print("  (no combined Bill 9 results)")
        return

    summaries_by_key: dict[tuple[str, str], dict[str, dict[str, str]]] = {}
    for row in collective_summaries:
        key = (row["scope"], row["tmk"])
        summaries_by_key.setdefault(key, {})[row["period"]] = row

    for (scope, tmk), periods in summaries_by_key.items():
        print(scope_label(scope, tmk))
        for period_key in (PERIOD_PRE_BILL9, PERIOD_POST_BILL9):
            row = periods.get(period_key)
            if row is None:
                continue
            analysis = PeriodAnalysis(
                scope=row["scope"],
                tmk=row["tmk"],
                period=row["period"],
                period_label=row["period_label"],
                period_start=parse_date(row["period_start"]),
                period_end=parse_date(row["period_end"]),
                transfer_count=int(row["transfer_count"]),
                priced_sale_count=int(row["priced_sale_count"]),
                transfers_per_year=float(row["transfers_per_year"] or 0),
                median_sale_price=float(row["median_sale_price"]) if row["median_sale_price"] else None,
                mean_sale_price=float(row["mean_sale_price"]) if row["mean_sale_price"] else None,
                hi_pct_transfers=float(row["hi_pct_transfers"] or 0),
                non_hi_pct_transfers=float(row["non_hi_pct_transfers"] or 0),
                unknown_pct_transfers=float(row["unknown_pct_transfers"] or 0),
                unique_units_transferred=int(row["unique_units_transferred"]),
                newly_resolved_units=int(row["newly_resolved_units"]),
                newly_resolved_hi_pct=float(row["newly_resolved_hi_pct"] or 0),
                newly_resolved_non_hi_pct=float(row["newly_resolved_non_hi_pct"] or 0),
                portfolio_hi_pct=float(row["portfolio_hi_pct"] or 0),
                portfolio_non_hi_pct=float(row["portfolio_non_hi_pct"] or 0),
                portfolio_unknown_pct=float(row["portfolio_unknown_pct"] or 0),
                non_hi_pct_high_price_transfers=float(row["non_hi_pct_high_price_transfers"])
                if row["non_hi_pct_high_price_transfers"]
                else None,
                non_hi_pct_low_price_transfers=float(row["non_hi_pct_low_price_transfers"])
                if row["non_hi_pct_low_price_transfers"]
                else None,
                high_price_transfer_count=int(row["high_price_transfer_count"]),
                low_price_transfer_count=int(row["low_price_transfer_count"]),
                notes=row["notes"],
            )
            print_period_summary(analysis)

        scope_comparisons = [
            row
            for row in collective_comparisons
            if row["scope"] == scope and row["tmk"] == tmk
        ]
        if scope_comparisons:
            print("  Changes (post vs pre Bill 9 window)")
            highlight_metrics = {
                "median_sale_price",
                "non_hi_pct_transfers",
                "portfolio_non_hi_pct",
                "newly_resolved_non_hi_pct",
                "non_hi_pct_high_price_transfers",
                "transfers_per_year",
            }
            for row in scope_comparisons:
                if row["metric"] not in highlight_metrics:
                    continue
                pre_val = row["pre_period_value"] or "n/a"
                post_val = row["post_period_value"] or "n/a"
                delta = row["delta"] or "n/a"
                pct = row["pct_change"]
                pct_label = f" ({pct}%)" if pct else ""
                print(
                    f"    {row['metric_label']}: {pre_val} → {post_val} "
                    f"(Δ {delta}{pct_label})"
                )
        print()


def run_bill9_period_analysis(
    results: list[TmkResult],
    data_root: Path,
    output_dir: Path,
    output_prefix: str,
    dry_run: bool,
) -> None:
    collective = analyze_collective_bill9_periods(results, data_root, output_prefix)
    if collective is None:
        logger.warning("Bill 9 period analysis skipped: no transfer data found")
        return

    summary_rows, comparison_rows, price_residency_rows = collective

    summary_path = output_dir / prefixed_output_name(output_prefix, "bill9-period-summary.csv")
    comparison_path = output_dir / prefixed_output_name(output_prefix, "bill9-period-comparison.csv")
    price_residency_path = output_dir / prefixed_output_name(
        output_prefix,
        "bill9-price-residency.csv",
    )
    write_csv(summary_path, BILL9_PERIOD_SUMMARY_COLUMNS, summary_rows, dry_run)
    write_csv(comparison_path, BILL9_PERIOD_COMPARISON_COLUMNS, comparison_rows, dry_run)
    write_csv(price_residency_path, BILL9_PRICE_RESIDENCY_COLUMNS, price_residency_rows, dry_run)
    print_bill9_period_analysis(
        summary_path,
        comparison_path,
        price_residency_path,
        summary_rows,
        comparison_rows,
        dry_run,
    )


@dataclass(frozen=True)
class UnitTaxRecord:
    tmk: str
    cpr: str
    unit: str
    parid: str
    tax_rate_class_code: str
    tax_rate_class_label: str
    land_class_code: str
    land_exemption: int
    building_exemption: int
    owner_address_region: str

    @property
    def has_any_exemption(self) -> bool:
        return self.land_exemption > 0 or self.building_exemption > 0

    @property
    def is_owner_occupied(self) -> bool:
        return is_owner_occupied_tax_class(self.tax_rate_class_code)

    @property
    def is_homestead_exemption(self) -> bool:
        return self.is_owner_occupied and self.has_any_exemption

    @property
    def is_ltr_exemption(self) -> bool:
        return is_long_term_rental_tax_class(self.tax_rate_class_code) and self.has_any_exemption


def pct_of_total(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(100.0 * count / total, 4)


def region_counts(records: list[UnitTaxRecord]) -> dict[str, int]:
    counts = {region: 0 for region in _ADDRESS_REGION_LABELS}
    for record in records:
        counts[record.owner_address_region] = counts.get(record.owner_address_region, 0) + 1
    return counts


def build_homestead_exemption_summary_row(
    unit_records: list[UnitTaxRecord],
    combined_tmks: str,
) -> dict[str, str]:
    total_units = len(unit_records)
    homestead_records = [record for record in unit_records if record.is_homestead_exemption]
    ltr_records = [record for record in unit_records if record.is_ltr_exemption]
    no_exemption_count = sum(1 for record in unit_records if not record.has_any_exemption)
    homestead_count = len(homestead_records)
    ltr_count = len(ltr_records)
    homestead_regions = region_counts(homestead_records)

    def region_fields(prefix: str, regions: dict[str, int], denominator: int) -> dict[str, str]:
        fields: dict[str, str] = {}
        for region in _ADDRESS_REGION_LABELS:
            count = regions.get(region, 0)
            fields[f"{prefix}_{region}_units"] = str(count)
            fields[f"{prefix}_{region}_pct"] = (
                f"{pct_of_total(count, denominator):.4f}" if denominator else "0.0000"
            )
        return fields

    row = {
        "scope": SCOPE_COLLECTIVE,
        "tmks": combined_tmks,
        "total_units": str(total_units),
        "homestead_units": str(homestead_count),
        "homestead_pct": f"{pct_of_total(homestead_count, total_units):.4f}",
        "ltr_exemption_units": str(ltr_count),
        "ltr_exemption_pct": f"{pct_of_total(ltr_count, total_units):.4f}",
        "no_exemption_units": str(no_exemption_count),
        "no_exemption_pct": f"{pct_of_total(no_exemption_count, total_units):.4f}",
    }
    row.update(region_fields("homestead", homestead_regions, homestead_count))
    return row


def unit_owner_address_region(unit: UnitResidency) -> str:
    regions = [
        classify_owner_address_region(
            owner.mailing_state,
            owner.mailing_city_state_zip,
            owner.country,
        )
        for owner in unit.owners
    ]
    shares = address_region_shares(regions)
    return max(
        shares.items(),
        key=lambda item: item[1],
    )[0]


def unit_residency_lookup(results: list[TmkResult]) -> dict[tuple[str, str], UnitResidency]:
    return {(unit.tmk, unit.cpr): unit for result in results for unit in result.units}


def load_unit_tax_records(
    tmk: str,
    data_root: Path,
    output_prefix: str,
    unit_lookup: dict[tuple[str, str], UnitResidency],
) -> list[UnitTaxRecord]:
    pardat_path = discover_tmk_file(data_root, "fullpardat26-{tmk}.txt", tmk, output_prefix)
    asmt_path = discover_tmk_file(data_root, "fullasmt26-{tmk}.txt", tmk, output_prefix)
    if pardat_path is None or asmt_path is None:
        missing = [
            label
            for label, path in (("fullpardat", pardat_path), ("fullasmt", asmt_path))
            if path is None
        ]
        logger.warning("skipping tax rate class load for TMK %s: missing %s", tmk, ", ".join(missing))
        return []

    units_index = build_unit_index(pardat_path, tmk)
    asmt_by_cpr: dict[str, dict[str, str]] = {}
    for row in read_csv_dicts(asmt_path):
        _, _, _, _, _, cpr = tmk_columns(row)
        if cpr == MASTER_CPR:
            continue
        asmt_by_cpr[cpr] = row

    records: list[UnitTaxRecord] = []
    for cpr, unit in sorted(units_index.items()):
        asmt_row = asmt_by_cpr.get(cpr, {})
        tax_rate_raw = asmt_row.get("TAX RATE CLASS", "")
        land_class_raw = asmt_row.get("LAND CLASS", "")
        tax_rate_code = normalize_tax_rate_class(tax_rate_raw)
        unit_residency = unit_lookup.get((tmk, cpr))
        owner_address_region = (
            unit_owner_address_region(unit_residency)
            if unit_residency is not None
            else "unknown"
        )
        land_exemption = parse_exemption_amount(asmt_row.get("LAND EXEMPTION", ""))
        building_exemption = parse_exemption_amount(asmt_row.get("BUILDING EXEMPTION", ""))
        records.append(
            UnitTaxRecord(
                tmk=tmk,
                cpr=cpr,
                unit=unit.unit,
                parid=unit.parid,
                tax_rate_class_code=tax_rate_code,
                tax_rate_class_label=tax_rate_class_label(tax_rate_raw),
                land_class_code=normalize_tax_rate_class(land_class_raw),
                land_exemption=land_exemption,
                building_exemption=building_exemption,
                owner_address_region=owner_address_region,
            )
        )
    return records


def build_tax_rate_class_summary_rows(
    unit_records: list[UnitTaxRecord],
    combined_tmks: str,
) -> list[dict[str, str]]:
    if not unit_records:
        return []

    class_counts: dict[tuple[str, str], int] = {}
    class_region_counts: dict[tuple[str, str], dict[str, int]] = {}
    class_exemption_counts: dict[tuple[str, str], int] = {}
    for record in unit_records:
        class_key = (record.tax_rate_class_code, record.tax_rate_class_label)
        class_counts[class_key] = class_counts.get(class_key, 0) + 1
        if record.has_any_exemption:
            class_exemption_counts[class_key] = class_exemption_counts.get(class_key, 0) + 1
        region_counts = class_region_counts.setdefault(class_key, {})
        region_counts[record.owner_address_region] = (
            region_counts.get(record.owner_address_region, 0) + 1
        )

    total_units = len(unit_records)
    rows: list[dict[str, str]] = []
    for class_key, count in sorted(
        class_counts.items(),
        key=lambda item: (
            -item[1],
            int(item[0][0]) if item[0][0].isdigit() else 999,
            item[0][1],
        ),
    ):
        unit_pct = round(100.0 * count / total_units, 4)
        region_counts = class_region_counts.get(class_key, {})
        exemption_count = class_exemption_counts.get(class_key, 0)
        exemption_pct = round(100.0 * exemption_count / count, 4) if count else 0.0
        row = {
            "scope": SCOPE_COLLECTIVE,
            "tmks": combined_tmks,
            "tax_rate_class_code": class_key[0] or "",
            "tax_rate_class_label": class_key[1],
            "unit_count": str(count),
            "unit_pct": f"{unit_pct:.4f}",
            "exemption_units": str(exemption_count),
            "exemption_pct_of_class": f"{exemption_pct:.4f}",
        }
        for region in ("hi", "usa", "foreign", "unknown"):
            region_count = region_counts.get(region, 0)
            region_pct = round(100.0 * region_count / count, 4) if count else 0.0
            row[f"{region}_units"] = str(region_count)
            row[f"{region}_pct_of_class"] = f"{region_pct:.4f}"
        rows.append(row)
    return rows


def unit_tax_records_to_rows(unit_records: list[UnitTaxRecord]) -> list[dict[str, str]]:
    return [
        {
            "tmk": record.tmk,
            "cpr": record.cpr,
            "unit": record.unit,
            "parid": record.parid,
            "tax_rate_class_code": record.tax_rate_class_code,
            "tax_rate_class_label": record.tax_rate_class_label,
            "land_class_code": record.land_class_code,
            "land_exemption": str(record.land_exemption),
            "building_exemption": str(record.building_exemption),
            "has_any_exemption": "yes" if record.has_any_exemption else "no",
            "is_owner_occupied": "yes" if record.is_owner_occupied else "no",
            "is_homestead_exemption": "yes" if record.is_homestead_exemption else "no",
            "is_ltr_exemption": "yes" if record.is_ltr_exemption else "no",
            "owner_address_region": record.owner_address_region,
        }
        for record in sorted(unit_records, key=lambda item: (item.tmk, item.unit, item.cpr))
    ]


def format_exemption_amount(amount: int) -> str:
    if amount <= 0:
        return "$0"
    return f"${amount:,}"


def print_homestead_exemption_summary(
    unit_records: list[UnitTaxRecord],
    total_units: int,
) -> None:
    homestead_records = sorted(
        [record for record in unit_records if record.is_homestead_exemption],
        key=lambda item: (item.tmk, item.unit, item.cpr),
    )
    ltr_records = [record for record in unit_records if record.is_ltr_exemption]
    no_exemption_count = sum(1 for record in unit_records if not record.has_any_exemption)
    homestead_count = len(homestead_records)
    ltr_count = len(ltr_records)

    print("Homestead exemption (homeowner) — all selected TMKs combined")
    print(HOMESTEAD_EXEMPTION_DISCLAIMER)
    print()
    print(
        f"  homestead exemption (tax class 9, owner-occupied): "
        f"{homestead_count} units ({pct_of_total(homestead_count, total_units)}%)"
    )
    if homestead_count:
        homestead_regions = region_counts(homestead_records)
        print("  owner mailing address on homestead units (proxy, not proof of occupancy):")
        for region in ("hi", "usa", "foreign", "unknown"):
            region_count = homestead_regions.get(region, 0)
            if region_count == 0:
                continue
            print(
                f"    {_ADDRESS_REGION_LABELS[region]}: {region_count} units "
                f"({pct_of_total(region_count, homestead_count)}%)"
            )
        print("  homestead units:")
        for record in homestead_records:
            exemption_parts: list[str] = []
            if record.land_exemption > 0:
                exemption_parts.append(f"land {format_exemption_amount(record.land_exemption)}")
            if record.building_exemption > 0:
                exemption_parts.append(
                    f"building {format_exemption_amount(record.building_exemption)}"
                )
            exemption_text = ", ".join(exemption_parts) if exemption_parts else "no exemption"
            print(
                f"    {record.unit} ({exemption_text}, "
                f"{_ADDRESS_REGION_LABELS.get(record.owner_address_region, record.owner_address_region)})"
            )
    print()
    print("Other property tax exemptions (not homestead)")
    print(
        f"  long-term rental exemption (tax class 12): "
        f"{ltr_count} units ({pct_of_total(ltr_count, total_units)}%)"
    )
    if ltr_count:
        for record in sorted(ltr_records, key=lambda item: (item.tmk, item.unit, item.cpr)):
            print(
                f"    {record.unit} (building {format_exemption_amount(record.building_exemption)}, "
                f"{_ADDRESS_REGION_LABELS.get(record.owner_address_region, record.owner_address_region)})"
            )
    print(
        f"  no land or building exemption: "
        f"{no_exemption_count} units ({pct_of_total(no_exemption_count, total_units)}%)"
    )
    print()


def print_tax_rate_class_summary(
    summary_path: Path,
    units_path: Path,
    homestead_summary_path: Path,
    summary_rows: list[dict[str, str]],
    unit_records: list[UnitTaxRecord],
    combined_tmks: str,
    total_units: int,
    dry_run: bool,
) -> None:
    action = "Would write" if dry_run else "Wrote"
    print(f"\n{action} tax rate class summary: {summary_path}")
    print(f"{action} tax rate class units: {units_path}")
    print(f"{action} homestead exemption summary: {homestead_summary_path}")
    print()
    print("Property tax rate class — all selected TMKs combined")
    print(TAX_RATE_CLASS_DISCLAIMER)
    print()
    print(f"All selected TMKs combined ({combined_tmks.replace(',', ', ')}) — {total_units} units")
    for row in summary_rows:
        print(
            f"  {row['tax_rate_class_code']} {row['tax_rate_class_label']}: "
            f"{row['unit_count']} units ({row['unit_pct']}%)"
        )
        for region in ("hi", "usa", "foreign", "unknown"):
            region_count = int(row[f"{region}_units"])
            if region_count == 0:
                continue
            region_label = _ADDRESS_REGION_LABELS[region]
            print(
                f"    {region_label}: {region_count} units "
                f"({row[f'{region}_pct_of_class']}%)"
            )
        exemption_count = int(row["exemption_units"])
        if exemption_count > 0:
            print(
                f"    any exemption: {exemption_count} units "
                f"({row['exemption_pct_of_class']}%)"
            )

    print_homestead_exemption_summary(unit_records, total_units)


def run_tax_rate_class_summary(
    results: list[TmkResult],
    data_root: Path,
    output_dir: Path,
    output_prefix: str,
    dry_run: bool,
) -> None:
    combined_tmks = combined_tmk_key(results)
    unit_lookup = unit_residency_lookup(results)
    unit_records: list[UnitTaxRecord] = []
    for result in results:
        unit_records.extend(
            load_unit_tax_records(result.tmk, data_root, output_prefix, unit_lookup)
        )

    if not unit_records:
        logger.warning("tax rate class summary skipped: no assessment data found")
        return

    summary_rows = build_tax_rate_class_summary_rows(unit_records, combined_tmks)
    unit_rows = unit_tax_records_to_rows(unit_records)

    summary_path = output_dir / prefixed_output_name(
        output_prefix,
        "tax-rate-class-summary.csv",
    )
    units_path = output_dir / prefixed_output_name(
        output_prefix,
        "tax-rate-class-units.csv",
    )
    homestead_summary_path = output_dir / prefixed_output_name(
        output_prefix,
        "homestead-exemption-summary.csv",
    )
    homestead_summary_row = build_homestead_exemption_summary_row(unit_records, combined_tmks)

    write_csv(summary_path, TAX_RATE_CLASS_SUMMARY_COLUMNS, summary_rows, dry_run)
    write_csv(units_path, TAX_RATE_CLASS_UNIT_COLUMNS, unit_rows, dry_run)
    write_csv(
        homestead_summary_path,
        HOMESTEAD_EXEMPTION_SUMMARY_COLUMNS,
        [homestead_summary_row],
        dry_run,
    )
    print_tax_rate_class_summary(
        summary_path,
        units_path,
        homestead_summary_path,
        summary_rows,
        unit_records,
        combined_tmks,
        len(unit_records),
        dry_run,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tmks", type=Path, required=True, help="Path to TMK key list (one per line)")
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/ownership-timeline"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--infer-coowner-residency",
        action="store_true",
        help="Deprecated: TMK-level address fallback is always enabled",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    print(f"TMK list file: {args.tmks}")
    output_prefix = args.tmks.stem
    tmks = load_tmks(args.tmks)
    logger.info("loaded %d TMK key(s): %s", len(tmks), ", ".join(tmks))

    results: list[TmkResult] = []
    timeline_rows: list[dict[str, str]] = []
    summary_rows: list[dict[str, str]] = []

    for tmk in tmks:
        logger.info("processing non-HI residency for TMK %s", tmk)
        result = process_tmk(
            tmk,
            args.data_root,
            args.output_dir,
            output_prefix,
            args.dry_run,
        )
        results.append(result)
        timeline_rows.extend(result.timeline_rows)
        summary_rows.append(result.summary_row)

    collective_timeline, collective_summary = build_collective_rows(results)
    timeline_rows = collective_timeline + timeline_rows
    summary_rows = [collective_summary, *summary_rows]
    annual_rows = build_annual_rows(results)

    timeline_path = args.output_dir / prefixed_output_name(
        output_prefix, "non-hi-ownership-timeline.csv"
    )
    summary_path = args.output_dir / prefixed_output_name(
        output_prefix, "non-hi-ownership-summary.csv"
    )
    annual_path = args.output_dir / prefixed_output_name(
        output_prefix, "non-hi-ownership-annual.csv"
    )
    unknown_residency_path = args.output_dir / prefixed_output_name(
        output_prefix, "non-hi-ownership-unknown-residency.csv"
    )
    unknown_residency_rows = collect_unknown_residency_rows(results)
    write_csv(timeline_path, TIMELINE_COLUMNS, timeline_rows, args.dry_run)
    write_csv(summary_path, SUMMARY_COLUMNS, summary_rows, args.dry_run)
    write_csv(annual_path, ANNUAL_COLUMNS, annual_rows, args.dry_run)
    write_csv(
        unknown_residency_path,
        UNKNOWN_RESIDENCY_COLUMNS,
        unknown_residency_rows,
        args.dry_run,
    )
    print_summary(
        results,
        collective_summary,
        timeline_path,
        summary_path,
        annual_path,
        unknown_residency_path,
        annual_rows,
        unknown_residency_rows,
        args.dry_run,
    )
    run_bill9_period_analysis(
        results,
        args.data_root,
        args.output_dir,
        output_prefix,
        args.dry_run,
    )
    run_tax_rate_class_summary(
        results,
        args.data_root,
        args.output_dir,
        output_prefix,
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
