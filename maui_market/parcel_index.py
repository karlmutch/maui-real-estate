"""Load condo unit rows from county fullpardat extracts for a TMK portfolio."""

from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from county_metadata import tmk_key_to_parid  # noqa: E402

MASTER_CPR = "0000"
DEFAULT_CONDOMINIUM_NAME = "MAUI KAMAOLE"
MIN_BUILDING_PHASE_VOTES = 2
MIN_TMK_PHASE_VOTES = 2


@dataclass(frozen=True)
class ParcelUnit:
    tmk_key: str
    cpr: str
    parid: str
    boc_tmk: str
    unit: str
    boc_unit: str
    condominium_name: str
    street_address: str
    division: str
    zone: str
    section: str
    plat: str
    parcel: str


def load_tmks(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"TMK file not found: {path}")
    keys = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not keys:
        raise ValueError(f"no TMK keys found in {path}")
    return keys


def discover_county_file(
    data_root: Path,
    tmk: str,
    output_prefix: str,
    source_stem: str,
) -> Path | None:
    for subdir in sorted(data_root.iterdir()):
        if not subdir.is_dir():
            continue
        candidates = (
            subdir / f"{output_prefix}-{source_stem}-{tmk}.txt",
            subdir / f"{source_stem}-{tmk}.txt",
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate

    for subdir in sorted(data_root.iterdir()):
        if not subdir.is_dir():
            continue
        selected = subdir / f"{output_prefix}-{source_stem}-selected.txt"
        if selected.is_file():
            return selected
    return None


def discover_pardat_file(data_root: Path, tmk: str, output_prefix: str) -> Path | None:
    return discover_county_file(data_root, tmk, output_prefix, "fullpardat26")


def discover_fulllegal_file(data_root: Path, tmk: str, output_prefix: str) -> Path | None:
    return discover_county_file(data_root, tmk, output_prefix, "fulllegal26")


def normalize_cpr(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits.zfill(4)[-4:]


def tmk_columns(row: dict[str, str]) -> tuple[str, str, str, str, str, str]:
    division = (row.get("DIVISION - TMK") or row.get("DIVISION-TMK") or "").strip()
    zone = (row.get("ZONE - TMK") or "").strip()
    section = (row.get("SECTION - TMK") or "").strip()
    plat = (row.get("PLAT - TMK") or "").strip()
    parcel = (row.get("PARCEL - TMK") or "").strip()
    cpr = normalize_cpr(row.get("CPR - TMK", ""))
    return division, zone, section, plat, parcel, cpr


def format_boc_tmk(
    division: str,
    zone: str,
    section: str,
    plat: str,
    parcel: str,
    cpr: str,
) -> str:
    """Format a BOC TMK like 2-3-9-004-082-0001."""
    return "-".join(
        [
            str(int(division)),
            str(int(zone)),
            str(int(section)),
            plat.zfill(3),
            parcel.zfill(3),
            normalize_cpr(cpr),
        ]
    )


def format_boc_unit(unit: str) -> str:
    """Normalize a county unit label to BOC format, e.g. G 101 -> G101."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", unit or "").upper()
    match = re.match(r"^([A-Z]{1,2})(\d{2,3})$", cleaned)
    if match:
        return f"{match.group(1)}{match.group(2)}"
    return cleaned


def extract_building_letter(unit: str, legal_description: str = "") -> str:
    unit_text = (unit or "").strip().upper()
    unit_match = re.match(r"^([A-Z])\s*[- ]?\s*\d", unit_text)
    if unit_match:
        return unit_match.group(1)
    legal_match = re.search(
        r"APT(?:\s+NO)?\s+([A-Z])-",
        (legal_description or "").upper(),
    )
    if legal_match:
        return legal_match.group(1)
    return ""


def parse_explicit_condominium_name(legal_description: str) -> str | None:
    text = (legal_description or "").upper()
    if not text:
        return None
    lc_match = re.search(r"MAUI KAMAOLE\s*\(LC\)", text)
    if lc_match:
        return re.sub(r"\s+", " ", lc_match.group(0).strip())
    if re.search(r"MAUI KAMAOLE\s+PHASE\s+III", text) or re.search(
        r"MAUI KAMAOLE\s+PH\s+III", text
    ):
        return "MAUI KAMAOLE PHASE III"
    if re.search(r"MAUI KAMAOLE\s+PHASE\s+II", text) or re.search(
        r"MAUI KAMAOLE\s+PH\s+II", text
    ):
        return "MAUI KAMAOLE PHASE II"
    if re.search(r"MAUI KAMAOLE\s+PHASE\s+I\b", text) or re.search(
        r"MAUI KAMAOLE\s+PH\s+I\b", text
    ):
        return "MAUI KAMAOLE PHASE I"
    return None


def parse_explicit_condominium_phase(legal_description: str) -> str | None:
    name = parse_explicit_condominium_name(legal_description)
    if name and "(LC)" not in name:
        return name
    return None


def build_tmk_phase_counts(
    legal_descriptions: dict[tuple[str, str], str],
) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for (tmk_key, _), legal in legal_descriptions.items():
        phase = parse_explicit_condominium_phase(legal)
        if phase:
            counts[tmk_key][phase] += 1
    return counts


def explicit_phase_is_supported(
    explicit: str,
    *,
    tmk_key: str,
    building: str,
    building_phase_map: dict[tuple[str, str], str] | None,
    tmk_phase_counts: dict[str, dict[str, int]] | None,
) -> bool:
    if "(LC)" in explicit:
        return True
    phase_key = (tmk_key, building)
    if (
        building
        and building_phase_map
        and building_phase_map.get(phase_key) == explicit
    ):
        return True
    if not tmk_key or not tmk_phase_counts:
        return True
    return tmk_phase_counts.get(tmk_key, {}).get(explicit, 0) >= MIN_TMK_PHASE_VOTES


def build_building_phase_map(
    legal_descriptions: dict[tuple[str, str], str],
) -> dict[tuple[str, str], str]:
    """Infer phase labels from explicit legal rows, scoped by TMK and building."""
    votes: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for (tmk_key, _), legal in legal_descriptions.items():
        building = extract_building_letter("", legal)
        phase = parse_explicit_condominium_phase(legal)
        if building and phase:
            votes[(tmk_key, building)][phase] += 1
    phase_map: dict[tuple[str, str], str] = {}
    for key, phase_counts in votes.items():
        if not phase_counts:
            continue
        phase, count = max(phase_counts.items(), key=lambda item: item[1])
        if count >= MIN_BUILDING_PHASE_VOTES:
            phase_map[key] = phase
    return phase_map


def parse_condominium_name(
    legal_description: str,
    *,
    unit: str = "",
    tmk_key: str = "",
    building_phase_map: dict[tuple[str, str], str] | None = None,
    tmk_phase_counts: dict[str, dict[str, int]] | None = None,
) -> str:
    explicit = parse_explicit_condominium_name(legal_description)
    building = extract_building_letter(unit, legal_description)
    phase_key = (tmk_key, building)

    if explicit and explicit_phase_is_supported(
        explicit,
        tmk_key=tmk_key,
        building=building,
        building_phase_map=building_phase_map,
        tmk_phase_counts=tmk_phase_counts,
    ):
        return explicit

    if building and building_phase_map and phase_key in building_phase_map:
        return building_phase_map[phase_key]

    text = (legal_description or "").upper()
    if "MAUI KAMAOLE" in text:
        return DEFAULT_CONDOMINIUM_NAME
    return DEFAULT_CONDOMINIUM_NAME


def format_street_address(row: dict[str, str]) -> str:
    parts: list[str] = []
    street_number = (row.get("STREET NUMBER") or "").strip()
    street_direction = (row.get("STREET DIRECTION") or "").strip()
    street = (row.get("STREET") or "").strip()
    suffix = (row.get("STREET NAME SUFFIX") or "").strip()
    unit = (row.get("UNIT") or "").strip()

    if street_number:
        parts.append(street_number)
    if street_direction:
        parts.append(street_direction)
    if street:
        parts.append(street)
    if suffix:
        parts.append(suffix)
    address = " ".join(parts)
    if unit and unit.upper() != "C396":
        address = f"{address} UNIT {unit}".strip()
    return address.upper()


def row_matches_tmk_key(row: dict[str, str], tmk_key: str) -> bool:
    division, zone, section, plat, parcel, _ = tmk_columns(row)
    components = f"{division}{zone}{section}{plat}{parcel}"
    if len(tmk_key) >= 9:
        return components.endswith(tmk_key[1:]) or components == tmk_key
    return components.endswith(tmk_key)


def read_csv_dicts(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8", errors="replace") as handle:
        return list(csv.DictReader(handle))


def load_legal_descriptions(
    tmks_path: Path,
    data_root: Path,
) -> dict[tuple[str, str], str]:
    output_prefix = tmks_path.stem
    descriptions: dict[tuple[str, str], str] = {}
    for tmk_key in load_tmks(tmks_path):
        legal_path = discover_fulllegal_file(data_root, tmk_key, output_prefix)
        if legal_path is None:
            continue
        for row in read_csv_dicts(legal_path):
            _, _, _, _, _, cpr = tmk_columns(row)
            if cpr == MASTER_CPR or not row_matches_tmk_key(row, tmk_key):
                continue
            legal = (row.get("LEGAL DESCRIPTION") or "").strip()
            if legal:
                descriptions[(tmk_key, cpr)] = legal
    return descriptions


def parcel_unit_from_row(
    row: dict[str, str],
    tmk_key: str,
    legal_descriptions: dict[tuple[str, str], str],
    building_phase_map: dict[tuple[str, str], str] | None = None,
    tmk_phase_counts: dict[str, dict[str, int]] | None = None,
) -> ParcelUnit | None:
    division, zone, section, plat, parcel, cpr = tmk_columns(row)
    if cpr == MASTER_CPR:
        return None
    if not row_matches_tmk_key(row, tmk_key):
        return None
    unit = (row.get("UNIT") or "").strip()
    legal = legal_descriptions.get((tmk_key, cpr), "")
    return ParcelUnit(
        tmk_key=tmk_key,
        cpr=cpr,
        parid=tmk_key_to_parid(tmk_key, cpr),
        boc_tmk=format_boc_tmk(division, zone, section, plat, parcel, cpr),
        unit=unit,
        boc_unit=format_boc_unit(unit),
        condominium_name=parse_condominium_name(
            legal,
            unit=unit,
            tmk_key=tmk_key,
            building_phase_map=building_phase_map,
            tmk_phase_counts=tmk_phase_counts,
        ),
        street_address=format_street_address(row),
        division=division,
        zone=zone,
        section=section,
        plat=plat,
        parcel=parcel,
    )


def load_parcel_units(tmks_path: Path, data_root: Path) -> list[ParcelUnit]:
    tmks = load_tmks(tmks_path)
    output_prefix = tmks_path.stem
    legal_descriptions = load_legal_descriptions(tmks_path, data_root)
    building_phase_map = build_building_phase_map(legal_descriptions)
    tmk_phase_counts = build_tmk_phase_counts(legal_descriptions)
    units: list[ParcelUnit] = []
    seen: set[tuple[str, str]] = set()

    for tmk_key in tmks:
        pardat_path = discover_pardat_file(data_root, tmk_key, output_prefix)
        if pardat_path is None:
            raise FileNotFoundError(
                f"fullpardat file not found for TMK {tmk_key} under {data_root}"
            )
        for row in read_csv_dicts(pardat_path):
            unit = parcel_unit_from_row(
                row, tmk_key, legal_descriptions, building_phase_map, tmk_phase_counts
            )
            if unit is None:
                continue
            key = (unit.tmk_key, unit.cpr)
            if key in seen:
                continue
            seen.add(key)
            units.append(unit)

    if not units:
        raise ValueError(f"no parcel units found for {tmks_path}")
    return units
