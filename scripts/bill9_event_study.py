#!/usr/bin/env python3
"""Generate Bill 9 event study report for Maui Kamaole (or other TMK selections)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from maui_market.bill9_charts import generate_bill9_charts  # noqa: E402
from maui_market.bill9_event_study import (  # noqa: E402
    load_study_context,
    run_analysis,
    write_study_csvs,
)
from maui_market.bill9_policy import load_bill9_policy  # noqa: E402
from maui_market.bill9_report import build_markdown_report  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT = Path("data/bill9-event-study")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tmks",
        type=Path,
        default=Path("data/maui-kamaole.tmks"),
        help="Path to TMK key list (one per line)",
    )
    parser.add_argument(
        "--controls",
        type=Path,
        nargs="*",
        default=[],
        help="Optional control complex .tmks files for difference-in-differences",
    )
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--policy",
        type=Path,
        default=None,
        help="Optional Bill 9 policy YAML (defaults to maui_market/config/bill9_policy.yaml)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def load_control_contexts(
    control_paths: list[Path],
    *,
    data_root: Path,
    policy,
    dry_run: bool,
) -> list:
    contexts = []
    for path in control_paths:
        try:
            contexts.append(
                load_study_context(
                    path,
                    data_root=data_root,
                    policy=policy,
                    dry_run=dry_run,
                )
            )
            logger.info("loaded control complex from %s", path)
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("skipping control %s: %s", path, exc)
    return contexts


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    policy = load_bill9_policy(args.policy)
    logger.info("loading study data from %s", args.tmks)
    ctx = load_study_context(
        args.tmks,
        data_root=args.data_root,
        policy=policy,
        dry_run=args.dry_run,
    )
    logger.info(
        "loaded %d transfers (%d priced) across %d units",
        len(ctx.events),
        sum(1 for event in ctx.events if event.price is not None),
        ctx.total_units,
    )

    control_contexts = load_control_contexts(
        args.controls,
        data_root=args.data_root,
        policy=policy,
        dry_run=args.dry_run,
    )
    if args.controls and not control_contexts:
        logger.info("no control complexes loaded — DiD analysis will be skipped")

    outputs = run_analysis(ctx, control_contexts=control_contexts or None)

    if args.dry_run:
        logger.info("dry run — skipping file writes")
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_study_csvs(args.output_dir, outputs)
    logger.info("wrote CSV outputs to %s", args.output_dir)

    chart_paths = generate_bill9_charts(ctx, outputs, args.output_dir)
    logger.info("wrote %d chart(s)", len(chart_paths))

    report_path = args.output_dir / "bill9-event-study-report.md"
    build_markdown_report(ctx, outputs, chart_paths, output_path=report_path)
    logger.info("wrote report to %s", report_path)

    print(f"Bill 9 event study complete: {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
