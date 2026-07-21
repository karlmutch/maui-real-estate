from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from maui_market.models import ComplexConfig, Listing

UNIT_CODE_RE = re.compile(r"^[A-Z]{1,2}\d{2,3}$")
STREET_NUMBER_RE = re.compile(r"\b(\d{4})\s+S(?:outh)?\s+Kihei\b", re.I)
URL_UNIT_RE = re.compile(r"/unit-([^/]+)/", re.I)
URL_STREET_RE = re.compile(r"/(\d{4})-S-Kihei-Rd", re.I)


@dataclass(frozen=True)
class UnitRegistry:
    street_number: str
    units: frozenset[str]

    def contains(self, unit: str) -> bool:
        return unit.upper() in self.units


def normalize_unit_code(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
    if not cleaned:
        return ""
    match = re.match(r"^([A-Z]{1,2})(\d{2,3})$", cleaned)
    if not match:
        return cleaned
    return f"{match.group(1)}{match.group(2)}"


def parse_unit_from_url(url: str) -> str:
    match = URL_UNIT_RE.search(url or "")
    if not match:
        return ""
    return normalize_unit_code(match.group(1))


def parse_street_number_from_url(url: str) -> str:
    match = URL_STREET_RE.search(url or "")
    return match.group(1) if match else ""


def parse_street_number_from_text(text: str) -> str:
    match = STREET_NUMBER_RE.search(text or "")
    return match.group(1) if match else ""


def listing_street_number(listing: Listing) -> str:
    for source in (listing.listing_url, listing.address, listing.description):
        number = parse_street_number_from_url(source) or parse_street_number_from_text(source)
        if number:
            return number
    return ""


def listing_matches_complex(listing: Listing, config: ComplexConfig) -> bool:
    street_number = listing_street_number(listing)
    if not street_number:
        return False
    return street_number == config.street_number


def canonical_address(unit: str, config: ComplexConfig) -> str:
    if unit:
        return f"{config.street_number} S Kihei Rd Unit {unit}, Kihei, HI 96753"
    return f"{config.street_number} S Kihei Rd, Kihei, HI 96753"


def resolve_listing_unit(
    listing: Listing,
    config: ComplexConfig,
    registry: UnitRegistry | None = None,
) -> str:
    candidates: list[tuple[str, str]] = []
    url_unit = parse_unit_from_url(listing.listing_url)
    if url_unit:
        candidates.append(("url", url_unit))
    address_unit = parse_unit(listing.address, config.address_pattern)
    if address_unit:
        candidates.append(("address", address_unit))
    description_unit = parse_unit(listing.description, config.address_pattern)
    if description_unit:
        candidates.append(("description", description_unit))

    chosen = ""
    for source, unit in candidates:
        normalized = normalize_unit_code(unit)
        if registry is not None and normalized in registry.units:
            chosen = normalized
            break
        if not chosen and UNIT_CODE_RE.match(normalized):
            chosen = normalized

    if url_unit and chosen and normalize_unit_code(url_unit) != chosen:
        chosen = normalize_unit_code(url_unit)

    return chosen


def parse_unit(address: str, pattern: str) -> str:
    if not address:
        return ""
    match = re.search(pattern, address)
    if not match:
        bare = re.search(r"\b([A-Z]{1,2})\s*[- ]?\s*(\d{2,3})\b", address, re.I)
        if bare:
            return normalize_unit_code(f"{bare.group(1)}{bare.group(2)}")
        return ""
    return normalize_unit_code(match.group(1))


def apply_listing_identity(
    listing: Listing,
    config: ComplexConfig,
    registry: UnitRegistry | None = None,
) -> Listing | None:
    if not listing_matches_complex(listing, config):
        return None

    unit = resolve_listing_unit(listing, config, registry)
    listing.unit = unit
    if unit or not listing.address:
        listing.address = canonical_address(unit, config)
    listing.price_per_sqft = listing.compute_price_per_sqft()
    return listing


def _discover_pardat_file(data_root: Path, output_prefix: str, tmk: str) -> Path | None:
    patterns = [
        f"{output_prefix}-fullpardat26-{tmk}.txt",
        f"fullpardat26-{tmk}.txt",
    ]
    for subdir in sorted(data_root.iterdir()):
        if not subdir.is_dir():
            continue
        for pattern in patterns:
            candidate = subdir / pattern
            if candidate.is_file():
                return candidate
    return None


def load_unit_registry(config: ComplexConfig) -> UnitRegistry | None:
    if not config.tmks_file:
        return None
    tmks_path = Path(config.tmks_file)
    if not tmks_path.is_file():
        tmks_path = Path(__file__).resolve().parents[1] / config.tmks_file
    if not tmks_path.is_file():
        return None

    data_root = Path(config.data_root)
    if not data_root.is_absolute():
        data_root = Path(__file__).resolve().parents[1] / data_root

    units: set[str] = set()
    for line in tmks_path.read_text(encoding="utf-8").splitlines():
        tmk = line.strip()
        if not tmk or tmk.startswith("#"):
            continue
        pardat_path = _discover_pardat_file(data_root, config.output_prefix, tmk)
        if pardat_path is None:
            continue
        with pardat_path.open(newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                street_number = (row.get("STREET NUMBER") or "").strip()
                if street_number != config.street_number:
                    continue
                unit_raw = (row.get("UNIT") or "").strip()
                if not unit_raw or unit_raw.upper() == "C396":
                    continue
                units.add(normalize_unit_code(unit_raw))

    return UnitRegistry(street_number=config.street_number, units=frozenset(units))
