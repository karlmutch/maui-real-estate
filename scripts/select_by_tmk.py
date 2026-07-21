#!/usr/bin/env python3
"""Select county data records matching TMK keys from data/tmks.txt."""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from county_metadata import (
    FileSchema,
    csv_row_matches_tmk,
    line_matches_tmk,
    load_schema,
    slice_fixed_width_line,
)

logger = logging.getLogger(__name__)

COUNTY_DATA_DIRS = frozenset({"county-property-and-parcel-full", "county-sales-data"})
COUNTY_SOURCE_STEM = re.compile(
    r"^(?:full(?:ag|asmt|ownr|legal|lndarclass|pardat)\d+|sales)$",
    re.IGNORECASE,
)


def load_tmks(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"TMK file not found: {path}")
    keys = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    if not keys:
        raise ValueError(f"no TMK keys found in {path}")
    return keys


def is_county_source_file(path: Path) -> bool:
    return bool(COUNTY_SOURCE_STEM.match(path.stem))


def discover_source_files(data_root: Path) -> list[Path]:
    files: list[Path] = []
    for subdir in sorted(data_root.iterdir()):
        if not subdir.is_dir():
            continue
        if subdir.name not in COUNTY_DATA_DIRS:
            continue
        for pattern in ("*.txt", "*.csv"):
            for path in sorted(subdir.glob(pattern)):
                if path.name == "tmks.txt":
                    continue
                if is_county_source_file(path):
                    files.append(path)
    return files


def output_path_for(source: Path, tmk: str, output_prefix: str) -> Path:
    return source.parent / f"{output_prefix}-{source.stem}-{tmk}{source.suffix}"


def selected_output_path(source: Path, output_prefix: str) -> Path:
    return source.parent / f"{output_prefix}-{source.stem}-selected{source.suffix}"


def process_fixed_width(
    source: Path,
    schema: FileSchema,
    tmks: list[str],
    output_prefix: str,
    dry_run: bool,
) -> dict[str, int]:
    counts = {tmk: 0 for tmk in tmks}
    writers: dict[str, csv.writer] = {}
    handles: list = []

    if not dry_run:
        for tmk in tmks:
            handle = output_path_for(source, tmk, output_prefix).open("w", newline="", encoding="utf-8")
            handles.append(handle)
            writers[tmk] = csv.writer(handle, lineterminator="\n")
            writers[tmk].writerow([col.name for col in schema.columns])

    with source.open(encoding="utf-8", errors="replace") as infile:
        for line in infile:
            if not line.strip():
                continue
            for tmk in tmks:
                if line_matches_tmk(line, tmk, schema):
                    counts[tmk] += 1
                    if not dry_run:
                        writers[tmk].writerow(slice_fixed_width_line(line.rstrip("\n\r"), schema))

    for handle in handles:
        handle.close()

    return counts


def process_csv(
    source: Path,
    schema: FileSchema,
    tmks: list[str],
    output_prefix: str,
    dry_run: bool,
) -> dict[str, int]:
    counts = {tmk: 0 for tmk in tmks}
    writers: dict[str, csv.writer] = {}
    handles: list = []

    if not dry_run:
        for tmk in tmks:
            handle = output_path_for(source, tmk, output_prefix).open("w", newline="", encoding="utf-8")
            handles.append(handle)
            writers[tmk] = csv.writer(handle, lineterminator="\n")
            writers[tmk].writerow([col.name for col in schema.columns])

    with source.open(newline="", encoding="utf-8", errors="replace") as infile:
        reader = csv.reader(infile)
        next(reader, None)
        for row in reader:
            if not row:
                continue
            for tmk in tmks:
                if csv_row_matches_tmk(row, tmk):
                    counts[tmk] += 1
                    if not dry_run:
                        writers[tmk].writerow(row)

    for handle in handles:
        handle.close()

    return counts


def write_selected_merge(
    source: Path,
    tmks: list[str],
    schema: FileSchema,
    counts: dict[str, int],
    output_prefix: str,
    dry_run: bool,
) -> int:
    if dry_run:
        return sum(counts.values())

    selected_path = selected_output_path(source, output_prefix)
    total = 0
    with selected_path.open("w", newline="", encoding="utf-8") as outfile:
        writer = csv.writer(outfile, lineterminator="\n")
        writer.writerow([col.name for col in schema.columns])
        for tmk in tmks:
            per_tmk_path = output_path_for(source, tmk, output_prefix)
            if not per_tmk_path.is_file():
                continue
            with per_tmk_path.open(newline="", encoding="utf-8") as infile:
                reader = csv.reader(infile)
                next(reader, None)
                for row in reader:
                    writer.writerow(row)
                    total += 1
    return total


def process_source(source: Path, tmks: list[str], output_prefix: str, dry_run: bool) -> None:
    schema = load_schema(source)
    if schema.kind == "csv":
        counts = process_csv(source, schema, tmks, output_prefix, dry_run)
    else:
        counts = process_fixed_width(source, schema, tmks, output_prefix, dry_run)

    total_selected = write_selected_merge(source, tmks, schema, counts, output_prefix, dry_run)

    for tmk, count in counts.items():
        status = "would write" if dry_run else "wrote"
        logger.info(
            "%s %s: %d rows for TMK %s",
            status,
            output_path_for(source, tmk, output_prefix).name,
            count,
            tmk,
        )
        if count == 0:
            logger.warning("no matches for %s in %s", tmk, source.name)

    logger.info(
        "%s %s: %d total rows",
        "would write" if dry_run else "wrote",
        selected_output_path(source, output_prefix).name,
        total_selected,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tmks", type=Path, required=True, help="Path to TMK key list (one per line)")
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--dry-run", action="store_true")
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
    sources = discover_source_files(args.data_root)
    if not sources:
        logger.warning("no source data files found under %s", args.data_root)
        return 0

    logger.info("loaded %d TMK key(s): %s", len(tmks), ", ".join(tmks))
    logger.info("processing %d source file(s)", len(sources))

    for source in sources:
        logger.info("processing %s", source)
        process_source(source, tmks, output_prefix, args.dry_run)

    return 0


if __name__ == "__main__":
    sys.exit(main())
