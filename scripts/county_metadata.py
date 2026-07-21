"""Parse Maui County metadata PDFs and slice fixed-width / CSV records."""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

FileKind = Literal["fixed_width", "csv"]


@dataclass(frozen=True)
class Column:
    name: str
    start: int  # 1-based position in fixed-width records
    size: int | None = None  # computed from next column when None


@dataclass(frozen=True)
class FileSchema:
    columns: tuple[Column, ...]
    kind: FileKind
    pdf_path: Path | None = None

    @property
    def header_line(self) -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="")
        writer.writerow([col.name for col in self.columns])
        return buffer.getvalue()

    def column_sizes(self) -> list[int]:
        sizes: list[int] = []
        for index, column in enumerate(self.columns):
            if column.size is not None:
                sizes.append(column.size)
                continue
            if index + 1 < len(self.columns):
                sizes.append(self.columns[index + 1].start - column.start)
            else:
                sizes.append(0)
        return sizes


FILE_PDF_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"fullasmt", re.I), "data_FULLASMT.pdf"),
    (re.compile(r"fullownr", re.I), "data_FULLOWNR.pdf"),
    (re.compile(r"fulllegal", re.I), "data_FULLLEG.pdf"),
    (re.compile(r"fulllndarclass", re.I), "data_FULLNDARCLASS.pdf"),
    (re.compile(r"fullpardat", re.I), "data_FULLPARDAT.pdf"),
    (re.compile(r"fullag", re.I), "data_FULLAG.pdf"),
    (re.compile(r"sales", re.I), "sales.pdf"),
]


def resolve_pdf_path(data_file: Path) -> Path | None:
    stem = data_file.stem
    for pattern, pdf_name in FILE_PDF_PATTERNS:
        if pattern.search(stem):
            candidate = data_file.parent / pdf_name
            if candidate.is_file():
                return candidate
    return None


def extract_pdf_text(pdf_path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _normalize_field_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip())


def _parse_size_token(token: str) -> int | None:
    token = token.strip()
    if re.fullmatch(r"\d+", token):
        return int(token)
    if re.fullmatch(r"\d+\.\d+", token):
        left, right = token.split(".", 1)
        return int(left) + 1 + int(right)
    return None


def _finalize_columns(raw: list[tuple[int, str, int | None]]) -> list[Column]:
    raw.sort(key=lambda item: item[0])
    columns: list[Column] = []
    for index, (start, name, size) in enumerate(raw):
        if index + 1 < len(raw):
            computed = raw[index + 1][0] - start
            columns.append(Column(name=name, start=start, size=size or computed))
        else:
            columns.append(Column(name=name, start=start, size=size))
    return columns


def parse_fixed_width_pdf(text: str) -> list[Column]:
    marker = re.search(r"Start\s+Field Name\s+Size", text, re.I)
    if not marker:
        raise ValueError("fixed-width field table header not found")

    section = text[marker.end() :]
    section = re.split(r"--\s*\d+\s+of\s+\d+\s*--", section, maxsplit=1)[0]

    raw: list[tuple[int, str, int | None]] = []
    pending_start: int | None = None
    pending_name_parts: list[str] = []

    row_re = re.compile(
        r"^(.+?)\s+(\d+(?:\.\d+)?)\s+(?:A/N|N|NULL)\b",
        re.MULTILINE,
    )
    start_only_re = re.compile(r"^(\d+)\s*$")
    start_with_rest_re = re.compile(r"^(\d+)\s+(.+)$", re.DOTALL)

    for line in section.splitlines():
        line = line.strip()
        if not line:
            continue

        start_only = start_only_re.match(line)
        if start_only:
            if pending_start is not None and pending_name_parts:
                logger.warning("dropping incomplete field at start %s", pending_start)
            pending_start = int(start_only.group(1))
            pending_name_parts = []
            continue

        start_with_rest = start_with_rest_re.match(line)
        if start_with_rest and pending_start is None:
            pending_start = int(start_with_rest.group(1))
            line = start_with_rest.group(2).strip()
        elif pending_start is not None and not start_with_rest:
            line = line.strip()
        elif start_with_rest:
            pending_start = int(start_with_rest.group(1))
            line = start_with_rest.group(2).strip()
        else:
            continue

        row_match = row_re.match(line)
        if row_match:
            name_parts = pending_name_parts + [_normalize_field_name(row_match.group(1))]
            name = _normalize_field_name(" ".join(name_parts))
            size = _parse_size_token(row_match.group(2))
            raw.append((pending_start, name, size))
            pending_start = None
            pending_name_parts = []
            continue

        if pending_start is not None:
            pending_name_parts.append(_normalize_field_name(line))

    if pending_start is not None and pending_name_parts:
        logger.warning("dropping trailing incomplete field at start %s", pending_start)

    if not raw:
        raise ValueError("no fixed-width columns parsed")
    return _finalize_columns(raw)


def parse_sales_pdf(text: str) -> list[Column]:
    marker = re.search(r"DATA FIELD INFORMATION", text, re.I)
    if not marker:
        raise ValueError("sales field table header not found")

    section = text[marker.end() :]
    section = re.split(r"--\s*\d+\s+of\s+\d+\s*--", section, maxsplit=1)[0]

    columns: list[Column] = []
    for line in section.splitlines():
        line = line.strip()
        if not line or line.upper().startswith("FIELD NAME"):
            continue

        match = re.match(r"^([A-Z0-9_]+)\s+(\d+)\s*-\s*(\d+|end)\b", line, re.I)
        if not match:
            continue

        name = match.group(1).upper()
        if name == "NOTE1":
            continue
        start = int(match.group(2))
        end_token = match.group(3).lower()
        if end_token == "end":
            size = None
        else:
            size = int(end_token) - start + 1
        columns.append(Column(name=name, start=start, size=size))

    if not columns:
        raise ValueError("no sales columns parsed")

    finalized: list[Column] = []
    for index, column in enumerate(columns):
        if column.size is not None:
            finalized.append(column)
            continue
        if index + 1 < len(columns):
            size = columns[index + 1].start - column.start
        else:
            size = 0
        finalized.append(Column(name=column.name, start=column.start, size=size))
    return finalized


def load_schema(data_file: Path) -> FileSchema:
    pdf_path = resolve_pdf_path(data_file)
    kind: FileKind = "csv" if data_file.suffix.lower() == ".csv" else "fixed_width"

    if pdf_path is None:
        raise FileNotFoundError(f"no metadata PDF found for {data_file.name}")

    try:
        text = extract_pdf_text(pdf_path)
        if kind == "csv":
            columns = parse_sales_pdf(text)
        else:
            columns = parse_fixed_width_pdf(text)
        return FileSchema(columns=tuple(columns), kind=kind, pdf_path=pdf_path)
    except Exception as exc:
        logger.warning("PDF parse failed for %s (%s); using fallback schema", pdf_path, exc)
        return _fallback_schema(data_file, pdf_path, kind)


def _fallback_schema(data_file: Path, pdf_path: Path, kind: FileKind) -> FileSchema:
    stem = data_file.stem.lower()
    fallbacks: dict[str, tuple[Column, ...]] = {
        "fullasmt": (
            Column("DIVISION-TMK", 1),
            Column("ZONE - TMK", 2),
            Column("SECTION - TMK", 3),
            Column("PLAT - TMK", 4),
            Column("PARCEL - TMK", 7),
            Column("CPR - TMK", 10),
            Column("PARCEL YEAR", 14),
            Column("LAND CLASS", 19),
            Column("TAX RATE CLASS", 23),
            Column("ASSESSED LAND VALUE", 27),
            Column("LAND EXEMPTION", 40),
            Column("ASSESSED BUILDING VALUE", 53),
            Column("BUILDING EXEMPTION", 66),
        ),
        "fullownr": (
            Column("DIVISION - TMK", 1),
            Column("ZONE - TMK", 2),
            Column("SECTION - TMK", 3),
            Column("PLAT - TMK", 4),
            Column("PARCEL - TMK", 7),
            Column("CPR - TMK", 10),
            Column("OWNER", 14),
            Column("OWNER TYPE", 54),
            Column("C/O MAILING ADDRESS", 94),
            Column("MAILING STREET ADDRESS", 215),
            Column("MAILING CITY STATE ZIP", 295),
            Column("MAILING CITY NAME", 387),
            Column("MAILING STATE", 427),
            Column("MAILING ZIP1", 429),
            Column("MAILING ZIP 2", 434),
            Column("COUNTRY", 438),
        ),
        "fulllegal": (
            Column("DIVISION - TMK", 1),
            Column("ZONE - TMK", 2),
            Column("SECTION - TMK", 3),
            Column("PLAT - TMK", 4),
            Column("PARCEL - TMK", 7),
            Column("CPR - TMK", 10),
            Column("TAX YEAR", 14),
            Column("ACRES", 19),
            Column("SQFT", 33),
            Column("LEGAL DESCRIPTION", 44),
        ),
        "fulllndarclass": (
            Column("DIVISION-TMK", 1),
            Column("ZONE - TMK", 2),
            Column("SECTION - TMK", 3),
            Column("PLAT - TMK", 4),
            Column("PARCEL - TMK", 7),
            Column("CPR - TMK", 10),
            Column("LAND CLASS", 14),
            Column("MULTIPLE CLASS FLAG", 18),
            Column("PARCEL YEAR", 19),
            Column("LAND AREA PER CLASS", 24),
            Column("LAND LINE", 36),
        ),
        "fullpardat": (
            Column("DIVISION - TMK", 1),
            Column("ZONE - TMK", 2),
            Column("SECTION - TMK", 3),
            Column("PLAT - TMK", 4),
            Column("PARCEL - TMK", 7),
            Column("CPR - TMK", 10),
            Column("PARCEL YEAR", 14),
            Column("MULTIPLE CLASS FLAG", 19),
            Column("STREET NUMBER PRE", 20),
            Column("STREET NUMBER", 22),
            Column("ADDITIONAL STREET NUMBER", 32),
            Column("STREET DIRECTION", 38),
            Column("STREET", 40),
            Column("STREET NAME SUFFIX", 70),
            Column("UNIT DESCRIPTION", 78),
            Column("UNIT", 88),
            Column("PARCEL ACRES", 99),
            Column("NEIGHBORHOOD CODE", 116),
        ),
        "fullag": (
            Column("DIVISION-TMK", 1),
            Column("ZONE - TMK", 2),
            Column("SECTION - TMK", 3),
            Column("PLAT - TMK", 4),
            Column("PARCEL - TMK", 7),
            Column("CPR - TMK", 10),
            Column("PARCEL YEAR", 14),
            Column("LAND CLASS", 19),
            Column("AGRICULTURAL USE", 23),
            Column("ACRES", 30),
            Column("CAMA LAND LINE", 43),
            Column("VALUE", 48),
        ),
        "sales": (
            Column("PARID", 1),
            Column("SALEDATE", 19),
            Column("PRICE", 30),
            Column("RECORDDATE", 41),
            Column("INSTRUNO", 52),
            Column("INSTRUTYPE", 73),
            Column("LANDCOURT_NO", 114),
            Column("CERT_NO", 155),
            Column("BOOK", 196),
            Column("PAGE", 205),
            Column("CONV_TAX", 214),
            Column("DOC_TYPE", 255),
            Column("VALCODE", 296),
            Column("VALCODE", 337),
            Column("SALETYP", 340),
        ),
    }

    for key, columns in fallbacks.items():
        if key in stem:
            finalized = _finalize_columns([(col.start, col.name, col.size) for col in columns])
            return FileSchema(columns=tuple(finalized), kind=kind, pdf_path=pdf_path)

    raise ValueError(f"no fallback schema for {data_file.name}")


def slice_fixed_width_line(line: str, schema: FileSchema) -> list[str]:
    sizes = schema.column_sizes()
    values: list[str] = []
    for column, size in zip(schema.columns, sizes, strict=True):
        start = column.start - 1
        if size <= 0:
            values.append(line[start:].rstrip("\n\r"))
            break
        end = start + size
        values.append(line[start:end].strip() if end <= len(line) else line[start:].strip())
    return values


def format_csv_row(values: list[str]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="")
    writer.writerow(values)
    return buffer.getvalue()


def line_matches_tmk(line: str, tmk_key: str, schema: FileSchema) -> bool:
    if schema.kind == "fixed_width":
        return line.startswith(tmk_key)

    parid = line.split(",", 1)[0].strip()
    return parid_matches_tmk(parid, tmk_key)


def parid_matches_tmk(parid: str, tmk_key: str) -> bool:
    if len(tmk_key) >= 12:
        return parid.startswith(tmk_key[-12:])
    if len(tmk_key) >= 9:
        return parid.startswith(tmk_key[1:])
    return parid.startswith(tmk_key)


def csv_row_matches_tmk(row: list[str], tmk_key: str) -> bool:
    if not row:
        return False
    return parid_matches_tmk(row[0].strip(), tmk_key)


def tmk_key_to_parid(tmk_key: str, cpr: str) -> str:
    """Build a 12-digit PARID prefix from a county TMK key and CPR suffix."""
    cpr_digits = re.sub(r"\D", "", cpr).zfill(4)[-4:]
    if len(tmk_key) >= 9:
        return f"{tmk_key[1:]}{cpr_digits}"
    return f"{tmk_key}{cpr_digits}"


def is_master_parid(parid: str) -> bool:
    return parid.strip().endswith("0000")


# Maui County TAX RATE CLASS / OVRCLASS codes and labels.
# Source: County of Maui Real Property Tax Certification, FY 2025-2026.
MAUI_TAX_RATE_CLASS_LABELS: dict[str, str] = {
    "0": "TIME SHARE",
    "1": "NON-OWNER-OCCUPIED",
    "2": "APARTMENT",
    "3": "COMMERCIAL",
    "4": "INDUSTRIAL",
    "5": "AGRICULTURAL",
    "6": "CONSERVATION",
    "7": "HOTEL/RESORT",
    "9": "OWNER-OCCUPIED",
    "10": "COMMERCIALIZED RESIDENTIAL",
    "11": "TVR-STRH",
    "12": "LONG TERM RENTAL",
}

OWNER_OCCUPIED_TAX_RATE_CLASS = "9"
LONG_TERM_RENTAL_TAX_RATE_CLASS = "12"


def parse_exemption_amount(value: str) -> int:
    digits = "".join(ch for ch in value if ch.isdigit())
    if not digits:
        return 0
    return int(digits)


def is_owner_occupied_tax_class(code: str) -> bool:
    return normalize_tax_rate_class(code) == OWNER_OCCUPIED_TAX_RATE_CLASS


def is_long_term_rental_tax_class(code: str) -> bool:
    return normalize_tax_rate_class(code) == LONG_TERM_RENTAL_TAX_RATE_CLASS


def normalize_tax_rate_class(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if not digits:
        return ""
    return str(int(digits))


def tax_rate_class_label(value: str) -> str:
    code = normalize_tax_rate_class(value)
    if not code:
        return "UNKNOWN"
    return MAUI_TAX_RATE_CLASS_LABELS.get(code, f"CLASS {code}")


_TRANSFER_INSTRUTYPES = frozenset(
    {
        "fee conveyance",
        "ownership correction",
    }
)

_TRANSFER_DOC_TYPES = frozenset(
    {
        "apartment deed",
        "deed",
        "quitclaim apartment deed",
        "quitclaim deed",
        "commissioner's deed",
        "ownership correction",
    }
)

_EXCLUDED_DOC_KEYWORDS = (
    "route slip",
    "easement",
    "cpr",
    "hpr",
    "declaration",
    "cancellation",
)


def is_ownership_transfer(instrutype: str, doc_type: str) -> bool:
    """Return True when a sales row likely reflects a title ownership change."""
    instr = instrutype.strip().lower()
    doc = doc_type.strip().lower()
    if any(keyword in doc for keyword in _EXCLUDED_DOC_KEYWORDS):
        return False
    if instr in _TRANSFER_INSTRUTYPES:
        return True
    return doc in _TRANSFER_DOC_TYPES


def normalize_event_date(sale_date: str, record_date: str) -> str:
    """Prefer SALEDATE unless it is the county placeholder 1900/01/01."""
    sale = sale_date.strip()
    record = record_date.strip()
    if sale and sale != "1900/01/01":
        return sale
    return record


Residency = Literal["hi", "non_hi", "unknown"]
ResidencyConfidence = Literal["high", "inferred", "unknown"]
OwnerAddressRegion = Literal["hi", "usa", "foreign", "unknown"]

_US_COUNTRY_VALUES = frozenset({"", "USA", "US", "UNITED STATES", "UNITED STATES OF AMERICA"})
_HI_ZIP_RE = re.compile(r"\b96[78]\d{2}\b")


@dataclass(frozen=True)
class OwnerResidencyResult:
    residency: Residency
    confidence: ResidencyConfidence


@dataclass(frozen=True)
class OwnerAddressEntry:
    cpr: str
    owner_name: str
    mailing_state: str
    mailing_city_state_zip: str
    country: str
    result: OwnerResidencyResult


def normalize_owner_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().upper())


def needs_residency_fallback(result: OwnerResidencyResult) -> bool:
    """True when residency should be filled from other ownership rows on the same TMK."""
    if result.residency == "unknown":
        return True
    return result.confidence == "inferred"


def classify_owner_residency_detail(
    mailing_state: str,
    mailing_city_state_zip: str,
    country: str,
) -> OwnerResidencyResult:
    """Classify an owner mailing address as Hawaii, non-Hawaii, or unknown."""
    state = mailing_state.strip().upper()
    country_value = country.strip().upper()
    city_state_zip = mailing_city_state_zip.strip().upper()

    if country_value and country_value not in _US_COUNTRY_VALUES:
        return OwnerResidencyResult("non_hi", "high")
    if state == "HI":
        return OwnerResidencyResult("hi", "high")
    if state:
        return OwnerResidencyResult("non_hi", "high")
    if city_state_zip:
        padded = f" {city_state_zip} "
        if " HI " in padded or re.search(r"\bHI\b", city_state_zip):
            return OwnerResidencyResult("hi", "high")
        if _HI_ZIP_RE.search(city_state_zip):
            return OwnerResidencyResult("hi", "inferred")
        return OwnerResidencyResult("non_hi", "high")
    return OwnerResidencyResult("unknown", "unknown")


def classify_owner_address_region(
    mailing_state: str,
    mailing_city_state_zip: str,
    country: str,
) -> OwnerAddressRegion:
    """Classify owner mailing address as Hawaii, other US, foreign, or unknown."""
    state = mailing_state.strip().upper()
    country_value = country.strip().upper()
    city_state_zip = mailing_city_state_zip.strip().upper()

    if country_value and country_value not in _US_COUNTRY_VALUES:
        return "foreign"
    if state == "HI":
        return "hi"
    if state:
        return "usa"
    if city_state_zip:
        padded = f" {city_state_zip} "
        if " HI " in padded or re.search(r"\bHI\b", city_state_zip):
            return "hi"
        if _HI_ZIP_RE.search(city_state_zip):
            return "hi"
        return "usa"
    return "unknown"


def address_region_shares(regions: list[OwnerAddressRegion]) -> dict[OwnerAddressRegion, float]:
    """Split a unit equally across owners and sum by mailing-address region."""
    if not regions:
        return {"hi": 0.0, "usa": 0.0, "foreign": 0.0, "unknown": 100.0}

    share = 100.0 / len(regions)
    totals: dict[OwnerAddressRegion, float] = {
        "hi": 0.0,
        "usa": 0.0,
        "foreign": 0.0,
        "unknown": 0.0,
    }
    for region in regions:
        totals[region] += share
    return totals


def classify_owner_residency(
    mailing_state: str,
    mailing_city_state_zip: str,
    country: str,
) -> Residency:
    """Classify an owner mailing address as Hawaii, non-Hawaii, or unknown."""
    return classify_owner_residency_detail(
        mailing_state,
        mailing_city_state_zip,
        country,
    ).residency


def residency_shares(residencies: list[Residency]) -> dict[Residency, float]:
    """Split a unit equally across owners and sum by residency."""
    if not residencies:
        return {"hi": 0.0, "non_hi": 0.0, "unknown": 100.0}

    share = 100.0 / len(residencies)
    totals = {"hi": 0.0, "non_hi": 0.0, "unknown": 0.0}
    for residency in residencies:
        totals[residency] += share
    return totals


def resolve_owner_residencies(
    residencies: list[Residency],
    *,
    infer_coowner_residency: bool = False,
) -> list[Residency]:
    """Optionally fill unknown co-owner residencies from known co-owners on the same unit."""
    return [
        item.residency
        for item in resolve_owner_residency_details(
            [OwnerResidencyResult(value, "unknown" if value == "unknown" else "high") for value in residencies],
            infer_coowner_residency=infer_coowner_residency,
        )
    ]


def resolve_owner_residency_details(
    details: list[OwnerResidencyResult],
    *,
    infer_coowner_residency: bool = False,
) -> list[OwnerResidencyResult]:
    """Optionally fill unknown co-owner residencies when all known owners agree."""
    if not details or not infer_coowner_residency:
        return list(details)

    known = [item for item in details if item.residency != "unknown"]
    if not known:
        return list(details)
    if len({item.residency for item in known}) != 1:
        return list(details)

    fill_residency = known[0].residency
    return [
        OwnerResidencyResult(fill_residency, "inferred")
        if item.residency == "unknown"
        else item
        for item in details
    ]


def _unanimous_clear_residency(
    entries: list[OwnerAddressEntry],
    results: dict[tuple[str, str], OwnerResidencyResult],
) -> Residency | None:
    clear_residencies = {
        results[(entry.cpr, normalize_owner_name(entry.owner_name))].residency
        for entry in entries
        if not needs_residency_fallback(results[(entry.cpr, normalize_owner_name(entry.owner_name))])
    }
    if len(clear_residencies) != 1:
        return None
    return next(iter(clear_residencies))


def resolve_tmk_owner_residency_details(
    entries: list[OwnerAddressEntry],
) -> dict[tuple[str, str], OwnerResidencyResult]:
    """Fill unclear owner residency from other rows on the same TMK (unit co-owners, then same name)."""
    results: dict[tuple[str, str], OwnerResidencyResult] = {
        (entry.cpr, normalize_owner_name(entry.owner_name)): entry.result
        for entry in entries
    }

    by_cpr: dict[str, list[OwnerAddressEntry]] = {}
    for entry in entries:
        by_cpr.setdefault(entry.cpr, []).append(entry)

    for cpr_entries in by_cpr.values():
        fill_residency = _unanimous_clear_residency(cpr_entries, results)
        if fill_residency is None:
            continue
        for entry in cpr_entries:
            key = (entry.cpr, normalize_owner_name(entry.owner_name))
            if needs_residency_fallback(results[key]):
                results[key] = OwnerResidencyResult(fill_residency, "inferred")

    by_name: dict[str, list[OwnerAddressEntry]] = {}
    for entry in entries:
        by_name.setdefault(normalize_owner_name(entry.owner_name), []).append(entry)

    for name_entries in by_name.values():
        fill_residency = _unanimous_clear_residency(name_entries, results)
        if fill_residency is None:
            continue
        for entry in name_entries:
            key = (entry.cpr, normalize_owner_name(entry.owner_name))
            if needs_residency_fallback(results[key]):
                results[key] = OwnerResidencyResult(fill_residency, "inferred")

    return results


_OWNRADDR_OWNER_START = 13
_OWNRADDR_OWNER_LEN = 40
_OWNRADDR_CITY_STATE_ZIP_START = 295
_OWNRADDR_CITY_STATE_ZIP_LEN = 92
_OWNRADDR_STATE_START = 427
_OWNRADDR_STATE_LEN = 2
_OWNRADDR_COUNTRY_START = 438
_OWNRADDR_COUNTRY_LEN = 30


def parse_ownraddr_line(line: str) -> tuple[str, str, str, str, str] | None:
    """Return (tmk_cpr_prefix, owner, mailing_state, city_state_zip, country) from a fixed-width row."""
    if len(line) < _OWNRADDR_COUNTRY_START + _OWNRADDR_COUNTRY_LEN:
        return None
    prefix = line[:13].strip()
    owner = line[_OWNRADDR_OWNER_START : _OWNRADDR_OWNER_START + _OWNRADDR_OWNER_LEN].strip()
    if not prefix or not owner:
        return None
    city_state_zip = line[
        _OWNRADDR_CITY_STATE_ZIP_START : _OWNRADDR_CITY_STATE_ZIP_START + _OWNRADDR_CITY_STATE_ZIP_LEN
    ].strip()
    state = line[_OWNRADDR_STATE_START : _OWNRADDR_STATE_START + _OWNRADDR_STATE_LEN].strip()
    country = line[_OWNRADDR_COUNTRY_START : _OWNRADDR_COUNTRY_START + _OWNRADDR_COUNTRY_LEN].strip()
    return prefix, owner, state, city_state_zip, country


def load_ownraddr_supplement_for_tmk(path: Path, tmk_key: str) -> dict[tuple[str, str], dict[str, str]]:
    """Index supplemental mailing addresses from ownraddr.txt by (cpr, owner name)."""
    supplement: dict[tuple[str, str], dict[str, str]] = {}
    if not path.is_file():
        return supplement

    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.startswith(tmk_key):
                continue
            parsed = parse_ownraddr_line(line.rstrip("\n"))
            if parsed is None:
                continue
            prefix, owner, state, city_state_zip, country = parsed
            if len(prefix) < 13:
                continue
            cpr = prefix[-4:]
            if not any((state, city_state_zip, country)):
                continue
            supplement[(cpr, normalize_owner_name(owner))] = {
                "mailing_state": state,
                "mailing_city_state_zip": city_state_zip,
                "country": country,
            }
    return supplement


def effective_owner_mailing_fields(
    cpr: str,
    owner_name: str,
    row: dict[str, str],
    supplement: dict[tuple[str, str], dict[str, str]],
) -> tuple[str, str, str]:
    state = row.get("MAILING STATE", "").strip()
    city_state_zip = row.get("MAILING CITY STATE ZIP", "").strip()
    country = row.get("COUNTRY", "").strip()
    if state or city_state_zip or country:
        return state, city_state_zip, country

    extra = supplement.get((cpr, normalize_owner_name(owner_name)))
    if extra is None:
        return state, city_state_zip, country
    return (
        extra.get("mailing_state", "").strip(),
        extra.get("mailing_city_state_zip", "").strip(),
        extra.get("country", "").strip(),
    )
