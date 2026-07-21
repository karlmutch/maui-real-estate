from __future__ import annotations

import csv
import logging
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats

from maui_market.bill9_policy import (
    Bill9Policy,
    date_in_window,
    era_for_date,
    load_bill9_policy,
    window_end,
)

from maui_market.bill9_counterfactual import (
    COUNTERFACTUAL_COLUMNS,
    CounterfactualOutputs,
    build_counterfactual,
    counterfactual_summary_rows,
)

logger = logging.getLogger(__name__)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

BILL9_DISCLAIMER = (
    "This event study presents observed market patterns around Maui County Bill 9 milestones. "
    "Residency uses current fullownr mailing addresses as a proxy for owners — not historical "
    "buyer residency at each sale. Price metrics use arm's-length fee conveyances above $10,000. "
    "Correlation does not establish causation; external factors include mortgage rates, insurance "
    "costs, wildfire recovery, tourism demand, and broader Maui housing conditions."
)

LIMITATIONS_TEXT = """\
- Residency is inferred from current mailing address rather than historical residence at purchase.
- Mailing address is a proxy and may not reflect actual occupancy.
- Correlation does not establish causation.
- External factors include mortgage rates, insurance costs, wildfire recovery, tourism demand, and broader Maui housing conditions.
- Results are limited to Maui Kamaole and should not be generalized to all South Maui properties.
- Interior square footage and floor-plan attributes are not available in county extracts; comparable-unit analysis uses TMK, building letter, and assessed building-value bucket proxies. Price-per-square-foot metrics are omitted when sqft is unavailable.
"""


def _ownership_timeline():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import ownership_timeline as ot  # noqa: PLC0415

    return ot


@dataclass(frozen=True)
class EnrichedEvent:
    tmk: str
    parid: str
    cpr: str
    unit: str
    building: str
    sale_date: date
    year: int
    month: str
    price: float | None
    building_value_bucket: str
    hi_pct: float
    non_hi_pct: float
    unknown_pct: float


@dataclass
class StudyContext:
    policy: Bill9Policy
    combined_tmks: str
    total_units: int
    units: list[Any]
    units_by_parid: dict[str, Any]
    event_dates_by_parid: dict[str, list[str]]
    events: list[EnrichedEvent]
    today: date = field(default_factory=date.today)


def parse_unit_building(unit_label: str) -> str:
    token = unit_label.strip().split()[0] if unit_label.strip() else ""
    return token.split("-")[0].rstrip("-").upper()


def dominant_residency(hi_pct: float, non_hi_pct: float, unknown_pct: float) -> str:
    if hi_pct >= non_hi_pct and hi_pct >= unknown_pct:
        return "hi"
    if non_hi_pct >= unknown_pct:
        return "non_hi"
    return "unknown"


def format_date(value: date) -> str:
    return value.strftime("%Y/%m/%d")


def format_optional_float(value: float | None, *, digits: int = 2) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def format_pct(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.4f}"


def median_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def mean_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.mean(values))


def stddev_or_none(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return float(statistics.stdev(values))


def write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def enrich_event(event: Any, unit: Any | None) -> EnrichedEvent:
    ot = _ownership_timeline()
    sale_date = ot.parse_date(event.sale_date)
    hi = unit.hi_pct if unit else 0.0
    non_hi = unit.non_hi_pct if unit else 0.0
    unknown = unit.unknown_pct if unit else 100.0
    return EnrichedEvent(
        tmk=event.tmk,
        parid=event.parid,
        cpr=event.cpr,
        unit=event.unit,
        building=parse_unit_building(event.unit),
        sale_date=sale_date,
        year=sale_date.year,
        month=sale_date.strftime("%Y-%m"),
        price=event.price,
        building_value_bucket=event.building_value_bucket,
        hi_pct=hi,
        non_hi_pct=non_hi,
        unknown_pct=unknown,
    )


def load_study_context(
    tmks_path: Path,
    *,
    data_root: Path = Path("data"),
    policy: Bill9Policy | None = None,
    dry_run: bool = False,
) -> StudyContext:
    ot = _ownership_timeline()
    policy = policy or load_bill9_policy()
    output_prefix = tmks_path.stem
    tmks = ot.load_tmks(tmks_path)
    today = date.today()

    results: list[Any] = []
    for tmk in tmks:
        result = ot.process_tmk(
            tmk,
            data_root,
            data_root / "ownership-timeline",
            output_prefix,
            dry_run,
        )
        results.append(result)

    all_units = [unit for result in results for unit in result.units]
    units_by_parid = {unit.parid: unit for unit in all_units}
    all_event_dates: dict[str, list[str]] = {}
    raw_events: list[Any] = []

    for result in results:
        loaded = ot.load_tmk_transfer_events(result.tmk, data_root, output_prefix)
        if loaded is None:
            continue
        events, _counts = loaded
        raw_events.extend(events)
        all_event_dates.update(result.event_dates_by_parid)

    enriched: list[EnrichedEvent] = []
    for event in raw_events:
        sale_date = ot.parse_date(event.sale_date)
        if sale_date < policy.analysis_start or sale_date > today:
            continue
        unit = units_by_parid.get(event.parid)
        enriched.append(enrich_event(event, unit))

    enriched.sort(key=lambda item: (item.sale_date, item.parid))

    return StudyContext(
        policy=policy,
        combined_tmks=ot.combined_tmk_key(results),
        total_units=len(all_units),
        units=all_units,
        units_by_parid=units_by_parid,
        event_dates_by_parid=all_event_dates,
        events=enriched,
        today=today,
    )


def iter_months(start: date, end: date) -> list[str]:
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


def events_in_month(events: list[EnrichedEvent], month: str) -> list[EnrichedEvent]:
    return [event for event in events if event.month == month]


def priced_events(events: list[EnrichedEvent]) -> list[EnrichedEvent]:
    return [event for event in events if event.price is not None]


def rolling_median(values: list[float | None], window: int) -> list[float | None]:
    result: list[float | None] = []
    for index in range(len(values)):
        window_values = [
            value
            for value in values[max(0, index - window + 1) : index + 1]
            if value is not None
        ]
        result.append(median_or_none(window_values))
    return result


MONTHLY_MARKET_COLUMNS = [
    "month",
    "transfer_count",
    "arms_length_sale_count",
    "median_price",
    "mean_price",
    "median_ppsf",
    "mean_ppsf",
    "total_volume",
    "rolling_3mo_median_price",
    "annualized_transfer_rate",
    "price_index",
    "median_price_yoy_pct",
    "rolling_12mo_appreciation_pct",
    "notes",
]

MONTHLY_RESIDENCY_COLUMNS = [
    "month",
    "transfer_count",
    "hi_count",
    "non_hi_count",
    "unknown_count",
    "hi_pct",
    "non_hi_pct",
    "unknown_pct",
    "rolling_3mo_hi_pct",
    "rolling_3mo_non_hi_pct",
    "notes",
]

SUMMARY_COLUMNS = [
    "metric",
    "value",
    "notes",
]

PRICE_DISTRIBUTION_COLUMNS = [
    "cohort",
    "cohort_start",
    "cohort_end",
    "sale_count",
    "q1",
    "median",
    "q3",
    "mean",
    "stddev",
    "notes",
]

REPEAT_SALES_COLUMNS = [
    "parid",
    "unit",
    "tmk",
    "building",
    "prior_sale_date",
    "current_sale_date",
    "prior_price",
    "current_price",
    "appreciation_pct",
    "annualized_appreciation_pct",
    "days_between",
    "era",
    "notes",
]

COMPARABLE_UNITS_COLUMNS = [
    "month",
    "tmk",
    "building",
    "building_value_bucket",
    "sale_count",
    "median_price",
    "mean_price",
    "notes",
]

TURNOVER_COLUMNS = [
    "period_type",
    "period_label",
    "period_start",
    "period_end",
    "year",
    "total_units",
    "transfer_count",
    "unique_units_transferred",
    "turnover_rate",
    "avg_ownership_duration_days",
    "first_time_owner_transfers",
    "repeat_owner_transfers",
    "median_days_between_sales",
    "sales_per_month",
    "inventory_turnover",
    "notes",
]

STATISTICS_COLUMNS = [
    "test_name",
    "variable",
    "sample_a",
    "sample_b",
    "n_a",
    "n_b",
    "statistic",
    "p_value",
    "significant_05",
    "interpretation",
    "notes",
]


def build_monthly_market_rows(ctx: StudyContext) -> list[dict[str, str]]:
    months = iter_months(ctx.policy.analysis_start, ctx.today)
    median_by_month: list[float | None] = []
    baseline_median: float | None = None
    rows: list[dict[str, str]] = []

    for month in months:
        month_events = events_in_month(ctx.events, month)
        priced = priced_events(month_events)
        prices = [event.price for event in priced if event.price is not None]
        median_price = median_or_none(prices)  # type: ignore[arg-type]
        median_by_month.append(median_price)
        if month == "2019-01" and median_price is not None:
            baseline_median = median_price

    rolling_medians = rolling_median(median_by_month, 3)
    median_lookup = dict(zip(months, median_by_month, strict=True))

    for index, month in enumerate(months):
        month_events = events_in_month(ctx.events, month)
        priced = priced_events(month_events)
        prices = [event.price for event in priced if event.price is not None]
        median_price = median_or_none(prices)  # type: ignore[arg-type]
        mean_price = mean_or_none(prices)  # type: ignore[arg-type]
        total_volume = sum(prices) if prices else None

        year_str, month_str = month.split("-")
        prior_year_month = f"{int(year_str) - 1}-{month_str}"
        prior_median = median_lookup.get(prior_year_month)
        yoy_pct = None
        if median_price is not None and prior_median is not None and prior_median > 0:
            yoy_pct = 100.0 * (median_price - prior_median) / prior_median

        price_index = None
        if median_price is not None and baseline_median is not None and baseline_median > 0:
            price_index = 100.0 * median_price / baseline_median

        rolling_12mo = None
        if index >= 12:
            current = median_by_month[index]
            past = median_by_month[index - 12]
            if current is not None and past is not None and past > 0:
                rolling_12mo = 100.0 * (current - past) / past

        rows.append(
            {
                "month": month,
                "transfer_count": str(len(month_events)),
                "arms_length_sale_count": str(len(priced)),
                "median_price": format_optional_float(median_price, digits=0),
                "mean_price": format_optional_float(mean_price, digits=0),
                "median_ppsf": "",
                "mean_ppsf": "",
                "total_volume": format_optional_float(total_volume, digits=0),
                "rolling_3mo_median_price": format_optional_float(
                    rolling_medians[index], digits=0
                ),
                "annualized_transfer_rate": format_optional_float(
                    len(month_events) * 12.0, digits=2
                ),
                "price_index": format_optional_float(price_index, digits=4),
                "median_price_yoy_pct": format_optional_float(yoy_pct, digits=4),
                "rolling_12mo_appreciation_pct": format_optional_float(rolling_12mo, digits=4),
                "notes": "ppsf omitted — sqft unavailable in county extract",
            }
        )
    return rows


def build_monthly_residency_rows(ctx: StudyContext) -> list[dict[str, str]]:
    months = iter_months(ctx.policy.analysis_start, ctx.today)
    hi_pcts: list[float | None] = []
    non_hi_pcts: list[float | None] = []
    rows: list[dict[str, str]] = []

    for month in months:
        month_events = events_in_month(ctx.events, month)
        hi_count = non_hi_count = unknown_count = 0
        for event in month_events:
            category = dominant_residency(event.hi_pct, event.non_hi_pct, event.unknown_pct)
            if category == "hi":
                hi_count += 1
            elif category == "non_hi":
                non_hi_count += 1
            else:
                unknown_count += 1
        total = len(month_events)
        hi_pct = 100.0 * hi_count / total if total else None
        non_hi_pct = 100.0 * non_hi_count / total if total else None
        unknown_pct = 100.0 * unknown_count / total if total else None
        hi_pcts.append(hi_pct)
        non_hi_pcts.append(non_hi_pct)
        rows.append(
            {
                "month": month,
                "transfer_count": str(total),
                "hi_count": str(hi_count),
                "non_hi_count": str(non_hi_count),
                "unknown_count": str(unknown_count),
                "hi_pct": format_pct(hi_pct),
                "non_hi_pct": format_pct(non_hi_pct),
                "unknown_pct": format_pct(unknown_pct),
                "rolling_3mo_hi_pct": "",
                "rolling_3mo_non_hi_pct": "",
                "notes": "buyer residency proxy from current owner mailing address",
            }
        )

    rolling_hi = rolling_median(hi_pcts, 3)  # type: ignore[arg-type]
    rolling_non_hi = rolling_median(non_hi_pcts, 3)  # type: ignore[arg-type]
    for index, row in enumerate(rows):
        row["rolling_3mo_hi_pct"] = format_pct(rolling_hi[index])
        row["rolling_3mo_non_hi_pct"] = format_pct(rolling_non_hi[index])
    return rows


def build_summary_rows(ctx: StudyContext) -> list[dict[str, str]]:
    ot = _ownership_timeline()
    priced = priced_events(ctx.events)
    prices = [event.price for event in priced if event.price is not None]
    totals = ot.aggregate_residency_as_of(
        ctx.units, ctx.event_dates_by_parid, ctx.today
    )
    hi_pct, non_hi_pct, unknown_pct = totals.to_percentages()

    hi_transfers = non_hi_transfers = unknown_transfers = 0
    for event in ctx.events:
        category = dominant_residency(event.hi_pct, event.non_hi_pct, event.unknown_pct)
        if category == "hi":
            hi_transfers += 1
        elif category == "non_hi":
            non_hi_transfers += 1
        else:
            unknown_transfers += 1
    transfer_total = len(ctx.events)

    return [
        {
            "metric": "study_period_start",
            "value": format_date(ctx.policy.analysis_start),
            "notes": "",
        },
        {
            "metric": "study_period_end",
            "value": format_date(ctx.today),
            "notes": "",
        },
        {
            "metric": "tmks",
            "value": ctx.combined_tmks,
            "notes": "",
        },
        {
            "metric": "total_units",
            "value": str(ctx.total_units),
            "notes": "",
        },
        {
            "metric": "total_transfers",
            "value": str(len(ctx.events)),
            "notes": "ownership transfers in study period",
        },
        {
            "metric": "arms_length_sales",
            "value": str(len(priced)),
            "notes": "fee conveyances above $10,000",
        },
        {
            "metric": "median_sale_price",
            "value": format_optional_float(median_or_none(prices), digits=0),  # type: ignore[arg-type]
            "notes": "all study-period priced sales",
        },
        {
            "metric": "mean_sale_price",
            "value": format_optional_float(mean_or_none(prices), digits=0),  # type: ignore[arg-type]
            "notes": "",
        },
        {
            "metric": "portfolio_hi_pct",
            "value": format_pct(hi_pct),
            "notes": "current ownership proxy",
        },
        {
            "metric": "portfolio_non_hi_pct",
            "value": format_pct(non_hi_pct),
            "notes": "current ownership proxy",
        },
        {
            "metric": "portfolio_unknown_pct",
            "value": format_pct(unknown_pct),
            "notes": "",
        },
        {
            "metric": "transfer_hi_pct",
            "value": format_pct(100.0 * hi_transfers / transfer_total if transfer_total else 0),
            "notes": "dominant residency among transfers",
        },
        {
            "metric": "transfer_non_hi_pct",
            "value": format_pct(
                100.0 * non_hi_transfers / transfer_total if transfer_total else 0
            ),
            "notes": "",
        },
        {
            "metric": "disclaimer",
            "value": BILL9_DISCLAIMER,
            "notes": "",
        },
    ]


def cohort_prices(ctx: StudyContext, window_id: str) -> list[float]:
    window = next((item for item in ctx.policy.windows if item.id == window_id), None)
    if window is None:
        return []
    end = window_end(window, today=ctx.today)
    return [
        event.price
        for event in priced_events(ctx.events)
        if event.price is not None
        and window.start <= event.sale_date <= end
    ]


def build_price_distribution_rows(ctx: StudyContext) -> list[dict[str, str]]:
    cohort_labels = {
        "pre_announcement": "Before Bill 9 announcement",
        "post_announcement_pre_passage": "After announcement through passage",
        "post_passage": "After passage",
    }
    rows: list[dict[str, str]] = []
    for window in ctx.policy.windows:
        end = window_end(window, today=ctx.today)
        prices = cohort_prices(ctx, window.id)
        if not prices:
            rows.append(
                {
                    "cohort": cohort_labels.get(window.id, window.id),
                    "cohort_start": format_date(window.start),
                    "cohort_end": format_date(end),
                    "sale_count": "0",
                    "q1": "",
                    "median": "",
                    "q3": "",
                    "mean": "",
                    "stddev": "",
                    "notes": "no priced sales in window",
                }
            )
            continue
        quartiles = statistics.quantiles(prices, n=4)
        rows.append(
            {
                "cohort": cohort_labels.get(window.id, window.id),
                "cohort_start": format_date(window.start),
                "cohort_end": format_date(end),
                "sale_count": str(len(prices)),
                "q1": format_optional_float(quartiles[0], digits=0),
                "median": format_optional_float(median_or_none(prices), digits=0),
                "q3": format_optional_float(quartiles[2], digits=0),
                "mean": format_optional_float(mean_or_none(prices), digits=0),
                "stddev": format_optional_float(stddev_or_none(prices), digits=2),
                "notes": "arm's-length sales only",
            }
        )
    return rows


def build_repeat_sales_rows(ctx: StudyContext) -> list[dict[str, str]]:
    by_parid: dict[str, list[EnrichedEvent]] = {}
    for event in ctx.events:
        by_parid.setdefault(event.parid, []).append(event)

    rows: list[dict[str, str]] = []
    for parid, unit_events in by_parid.items():
        priced_unit_events = sorted(
            [event for event in unit_events if event.price is not None],
            key=lambda item: item.sale_date,
        )
        if len(priced_unit_events) < 2:
            continue
        for prior, current in zip(priced_unit_events, priced_unit_events[1:], strict=False):
            days_between = (current.sale_date - prior.sale_date).days
            if days_between <= 0 or prior.price is None or current.price is None:
                continue
            appreciation = 100.0 * (current.price - prior.price) / prior.price
            years = days_between / 365.25
            annualized = appreciation / years if years > 0 else None
            rows.append(
                {
                    "parid": parid,
                    "unit": current.unit,
                    "tmk": current.tmk,
                    "building": current.building,
                    "prior_sale_date": format_date(prior.sale_date),
                    "current_sale_date": format_date(current.sale_date),
                    "prior_price": format_optional_float(prior.price, digits=0),
                    "current_price": format_optional_float(current.price, digits=0),
                    "appreciation_pct": format_optional_float(appreciation, digits=4),
                    "annualized_appreciation_pct": format_optional_float(annualized, digits=4),
                    "days_between": str(days_between),
                    "era": era_for_date(current.sale_date, ctx.policy, today=ctx.today),
                    "notes": "",
                }
            )
    return rows


def build_comparable_unit_rows(ctx: StudyContext) -> list[dict[str, str]]:
    groups: dict[tuple[str, str, str, str], list[float]] = {}
    month_index: dict[tuple[str, str, str, str], str] = {}

    for event in priced_events(ctx.events):
        if event.price is None:
            continue
        key = (event.month, event.tmk, event.building, event.building_value_bucket)
        groups.setdefault(key, []).append(event.price)
        month_index[key] = event.month

    rows: list[dict[str, str]] = []
    for key in sorted(groups):
        month, tmk, building, bucket = key
        prices = groups[key]
        rows.append(
            {
                "month": month,
                "tmk": tmk,
                "building": building,
                "building_value_bucket": bucket,
                "sale_count": str(len(prices)),
                "median_price": format_optional_float(median_or_none(prices), digits=0),
                "mean_price": format_optional_float(mean_or_none(prices), digits=0),
                "notes": "grouped by TMK, building letter, assessed building-value bucket",
            }
        )
    return rows


def first_transfer_dates(ctx: StudyContext) -> dict[str, date]:
    ot = _ownership_timeline()
    result: dict[str, date] = {}
    for parid, dates in ctx.event_dates_by_parid.items():
        if not dates:
            continue
        result[parid] = ot.parse_date(dates[0])
    return result


def build_turnover_rows(ctx: StudyContext) -> list[dict[str, str]]:
    ot = _ownership_timeline()
    first_dates = first_transfer_dates(ctx)
    repeat_pairs = build_repeat_sales_rows(ctx)
    intervals = [int(row["days_between"]) for row in repeat_pairs if row["days_between"]]

    annual_rows: list[dict[str, str]] = []
    for year in range(ctx.policy.analysis_start.year, ctx.today.year + 1):
        year_events = [event for event in ctx.events if event.year == year]
        unique_units = {event.parid for event in year_events}
        first_time = sum(
            1
            for event in year_events
            if first_dates.get(event.parid) == event.sale_date
        )
        repeat_count = len(year_events) - first_time
        turnover_rate = len(unique_units) / ctx.total_units if ctx.total_units else 0.0
        annual_rows.append(
            {
                "period_type": "annual",
                "period_label": str(year),
                "period_start": format_date(date(year, 1, 1)),
                "period_end": format_date(
                    date(year, 12, 31) if year < ctx.today.year else ctx.today
                ),
                "year": str(year),
                "total_units": str(ctx.total_units),
                "transfer_count": str(len(year_events)),
                "unique_units_transferred": str(len(unique_units)),
                "turnover_rate": format_pct(100.0 * turnover_rate),
                "avg_ownership_duration_days": "",
                "first_time_owner_transfers": str(first_time),
                "repeat_owner_transfers": str(repeat_count),
                "median_days_between_sales": format_optional_float(
                    float(statistics.median(intervals)) if intervals else None, digits=0
                ),
                "sales_per_month": format_optional_float(
                    len(year_events) / 12.0, digits=2
                ),
                "inventory_turnover": format_pct(
                    100.0 * len(year_events) / ctx.total_units if ctx.total_units else 0
                ),
                "notes": "",
            }
        )

    window_rows: list[dict[str, str]] = []
    for window in ctx.policy.windows:
        end = window_end(window, today=ctx.today)
        window_events = [
            event for event in ctx.events if window.start <= event.sale_date <= end
        ]
        unique_units = {event.parid for event in window_events}
        months_span = max(
            1,
            (end.year - window.start.year) * 12
            + (end.month - window.start.month)
            + 1,
        )
        window_rows.append(
            {
                "period_type": "policy_window",
                "period_label": window.id,
                "period_start": format_date(window.start),
                "period_end": format_date(end),
                "year": "",
                "total_units": str(ctx.total_units),
                "transfer_count": str(len(window_events)),
                "unique_units_transferred": str(len(unique_units)),
                "turnover_rate": format_pct(
                    100.0 * len(unique_units) / ctx.total_units if ctx.total_units else 0
                ),
                "avg_ownership_duration_days": format_optional_float(
                    float(statistics.mean(intervals)) if intervals else None, digits=0
                ),
                "first_time_owner_transfers": "",
                "repeat_owner_transfers": "",
                "median_days_between_sales": format_optional_float(
                    float(statistics.median(intervals)) if intervals else None, digits=0
                ),
                "sales_per_month": format_optional_float(
                    len(window_events) / months_span, digits=2
                ),
                "inventory_turnover": format_pct(
                    100.0
                    * len(window_events)
                    / ctx.total_units
                    * (12.0 / months_span)
                    if ctx.total_units
                    else 0
                ),
                "notes": "liquidity metrics for policy window",
            }
        )
    return annual_rows + window_rows


def build_annual_ownership_residency_rows(ctx: StudyContext) -> list[dict[str, str]]:
    ot = _ownership_timeline()
    rows: list[dict[str, str]] = []
    for year in range(ctx.policy.analysis_start.year, ctx.today.year + 1):
        as_of = date(year, 12, 31) if year < ctx.today.year else ctx.today
        portfolio = ot.aggregate_residency_as_of(
            ctx.units, ctx.event_dates_by_parid, as_of
        )
        hi_pct, non_hi_pct, unknown_pct = portfolio.to_percentages()

        year_events = [event for event in ctx.events if event.year == year]
        hi_t = non_hi_t = unknown_t = 0
        for event in year_events:
            category = dominant_residency(event.hi_pct, event.non_hi_pct, event.unknown_pct)
            if category == "hi":
                hi_t += 1
            elif category == "non_hi":
                non_hi_t += 1
            else:
                unknown_t += 1
        transfer_total = len(year_events)
        rows.append(
            {
                "year": str(year),
                "as_of_date": format_date(as_of),
                "portfolio_hi_pct": format_pct(hi_pct),
                "portfolio_non_hi_pct": format_pct(non_hi_pct),
                "portfolio_unknown_pct": format_pct(unknown_pct),
                "transfer_hi_pct": format_pct(
                    100.0 * hi_t / transfer_total if transfer_total else 0
                ),
                "transfer_non_hi_pct": format_pct(
                    100.0 * non_hi_t / transfer_total if transfer_total else 0
                ),
                "transfer_unknown_pct": format_pct(
                    100.0 * unknown_t / transfer_total if transfer_total else 0
                ),
                "transfer_count": str(transfer_total),
                "notes": "portfolio residency from year-end proxy snapshot",
            }
        )
    return rows


ANNUAL_OWNERSHIP_RESIDENCY_COLUMNS = [
    "year",
    "as_of_date",
    "portfolio_hi_pct",
    "portfolio_non_hi_pct",
    "portfolio_unknown_pct",
    "transfer_hi_pct",
    "transfer_non_hi_pct",
    "transfer_unknown_pct",
    "transfer_count",
    "notes",
]

PRICE_VS_RESIDENCY_COLUMNS = [
    "year",
    "annual_median_price",
    "above_median_sale_count",
    "below_median_sale_count",
    "above_median_hi_pct",
    "above_median_non_hi_pct",
    "below_median_hi_pct",
    "below_median_non_hi_pct",
    "notes",
]


def build_price_vs_residency_rows(ctx: StudyContext) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for year in range(ctx.policy.analysis_start.year, ctx.today.year + 1):
        year_priced = [
            event for event in priced_events(ctx.events) if event.year == year
        ]
        if not year_priced:
            continue
        prices = [event.price for event in year_priced if event.price is not None]
        annual_median = median_or_none(prices)  # type: ignore[arg-type]
        if annual_median is None:
            continue

        above_hi = above_non_hi = below_hi = below_non_hi = 0
        above_count = below_count = 0
        for event in year_priced:
            if event.price is None:
                continue
            category = dominant_residency(event.hi_pct, event.non_hi_pct, event.unknown_pct)
            if event.price >= annual_median:
                above_count += 1
                if category == "hi":
                    above_hi += 1
                else:
                    above_non_hi += 1
            else:
                below_count += 1
                if category == "hi":
                    below_hi += 1
                else:
                    below_non_hi += 1

        rows.append(
            {
                "year": str(year),
                "annual_median_price": format_optional_float(annual_median, digits=0),
                "above_median_sale_count": str(above_count),
                "below_median_sale_count": str(below_count),
                "above_median_hi_pct": format_pct(
                    100.0 * above_hi / above_count if above_count else 0
                ),
                "above_median_non_hi_pct": format_pct(
                    100.0 * above_non_hi / above_count if above_count else 0
                ),
                "below_median_hi_pct": format_pct(
                    100.0 * below_hi / below_count if below_count else 0
                ),
                "below_median_non_hi_pct": format_pct(
                    100.0 * below_non_hi / below_count if below_count else 0
                ),
                "notes": "non_hi includes unknown-dominant transfers",
            }
        )
    return rows


def _window_by_id(policy: Bill9Policy, window_id: str):
    return next((item for item in policy.windows if item.id == window_id), None)


def mann_whitney_row(
    *,
    test_name: str,
    variable: str,
    sample_a: list[float],
    sample_b: list[float],
    label_a: str,
    label_b: str,
    notes: str = "",
) -> dict[str, str]:
    if len(sample_a) < 2 or len(sample_b) < 2:
        return {
            "test_name": test_name,
            "variable": variable,
            "sample_a": label_a,
            "sample_b": label_b,
            "n_a": str(len(sample_a)),
            "n_b": str(len(sample_b)),
            "statistic": "",
            "p_value": "",
            "significant_05": "no",
            "interpretation": "insufficient sample",
            "notes": notes,
        }
    result = stats.mannwhitneyu(sample_a, sample_b, alternative="two-sided")
    significant = result.pvalue < 0.05
    interpretation = (
        "reject equal distributions at alpha=0.05"
        if significant
        else "cannot reject equal distributions at alpha=0.05"
    )
    return {
        "test_name": test_name,
        "variable": variable,
        "sample_a": label_a,
        "sample_b": label_b,
        "n_a": str(len(sample_a)),
        "n_b": str(len(sample_b)),
        "statistic": format_optional_float(float(result.statistic), digits=4),
        "p_value": format_optional_float(float(result.pvalue), digits=6),
        "significant_05": "yes" if significant else "no",
        "interpretation": interpretation,
        "notes": notes,
    }


def build_statistics_rows(ctx: StudyContext, monthly_market: list[dict[str, str]]) -> list[dict[str, str]]:
    pre = _window_by_id(ctx.policy, "pre_announcement")
    if pre is None:
        return []

    announcement = next(
        (item for item in ctx.policy.milestones if item.id == "announcement"),
        None,
    )
    post_start = announcement.date if announcement else pre.end
    post_end = ctx.today

    pre_prices = [
        event.price
        for event in priced_events(ctx.events)
        if event.price is not None and pre.start <= event.sale_date <= pre.end
    ]
    post_prices = [
        event.price
        for event in priced_events(ctx.events)
        if event.price is not None and post_start <= event.sale_date <= post_end
    ]

    pre_months = [
        float(row["transfer_count"])
        for row in monthly_market
        if row["month"] < post_start.strftime("%Y-%m")
    ]
    post_months = [
        float(row["transfer_count"])
        for row in monthly_market
        if row["month"] >= post_start.strftime("%Y-%m")
    ]

    monthly_residency = build_monthly_residency_rows(ctx)
    pre_non_hi = [
        float(row["non_hi_pct"])
        for row in monthly_residency
        if row["non_hi_pct"] and row["month"] < post_start.strftime("%Y-%m")
    ]
    post_non_hi = [
        float(row["non_hi_pct"])
        for row in monthly_residency
        if row["non_hi_pct"] and row["month"] >= post_start.strftime("%Y-%m")
    ]

    rows = [
        mann_whitney_row(
            test_name="Mann-Whitney U",
            variable="sale_price",
            sample_a=pre_prices,  # type: ignore[arg-type]
            sample_b=post_prices,  # type: ignore[arg-type]
            label_a="pre_announcement",
            label_b="post_announcement",
            notes="arm's-length sale prices",
        ),
        mann_whitney_row(
            test_name="Mann-Whitney U",
            variable="monthly_transfer_count",
            sample_a=pre_months,
            sample_b=post_months,
            label_a="pre_announcement",
            label_b="post_announcement",
            notes="monthly ownership transfer counts",
        ),
        mann_whitney_row(
            test_name="Mann-Whitney U",
            variable="monthly_non_hi_pct",
            sample_a=pre_non_hi,
            sample_b=post_non_hi,
            label_a="pre_announcement",
            label_b="post_announcement",
            notes="dominant non-HI share among monthly transfers",
        ),
    ]

    post_priced = [
        event
        for event in priced_events(ctx.events)
        if event.price is not None and event.sale_date >= post_start
    ]
    if len(post_priced) >= 30:
        buckets = sorted({event.building_value_bucket for event in post_priced if event.building_value_bucket})
        bucket_to_index = {bucket: index for index, bucket in enumerate(buckets)}
        y: list[float] = []
        design: list[list[float]] = []
        for event in priced_events(ctx.events):
            if event.price is None or event.price <= 0:
                continue
            months = (event.sale_date.year - ctx.policy.analysis_start.year) * 12 + (
                event.sale_date.month - ctx.policy.analysis_start.month
            )
            post_indicator = 1.0 if event.sale_date >= post_start else 0.0
            row = [1.0, float(months), post_indicator]
            for bucket in buckets:
                row.append(1.0 if event.building_value_bucket == bucket else 0.0)
            design.append(row)
            y.append(float(np.log(event.price)))

        if len(y) > len(buckets) + 3:
            coeffs, residuals, rank, singular = np.linalg.lstsq(
                np.array(design), np.array(y), rcond=None
            )
            rows.append(
                {
                    "test_name": "OLS",
                    "variable": "log_sale_price",
                    "sample_a": "full_study",
                    "sample_b": "",
                    "n_a": str(len(y)),
                    "n_b": "",
                    "statistic": format_optional_float(float(coeffs[2]), digits=6),
                    "p_value": "",
                    "significant_05": "",
                    "interpretation": (
                        f"post_announcement coefficient on log(price); "
                        f"time_coef={coeffs[1]:.6f}"
                    ),
                    "notes": (
                        "log(price) ~ months_since_start + post_announcement + building_value_bucket; "
                        "no p-values (homoskedastic OLS); omitted variables include rates and tourism demand"
                    ),
                }
            )
    else:
        rows.append(
            {
                "test_name": "OLS",
                "variable": "log_sale_price",
                "sample_a": "post_announcement",
                "sample_b": "",
                "n_a": str(len(post_priced)),
                "n_b": "",
                "statistic": "",
                "p_value": "",
                "significant_05": "no",
                "interpretation": "skipped — fewer than 30 post-announcement priced sales",
                "notes": "",
            }
        )
    return rows


@dataclass
class StudyOutputs:
    summary: list[dict[str, str]]
    monthly_market: list[dict[str, str]]
    monthly_residency: list[dict[str, str]]
    price_distribution: list[dict[str, str]]
    repeat_sales: list[dict[str, str]]
    comparable_units: list[dict[str, str]]
    turnover: list[dict[str, str]]
    annual_ownership_residency: list[dict[str, str]]
    price_vs_residency: list[dict[str, str]]
    statistics: list[dict[str, str]]
    counterfactual: CounterfactualOutputs


def run_analysis(
    ctx: StudyContext,
    *,
    control_contexts: list[StudyContext] | None = None,
) -> StudyOutputs:
    monthly_market = build_monthly_market_rows(ctx)
    monthly_residency = build_monthly_residency_rows(ctx)
    counterfactual = build_counterfactual(ctx, monthly_market, control_contexts)
    summary = build_summary_rows(ctx)
    summary.extend(counterfactual_summary_rows(counterfactual.headline))
    return StudyOutputs(
        summary=summary,
        monthly_market=monthly_market,
        monthly_residency=monthly_residency,
        price_distribution=build_price_distribution_rows(ctx),
        repeat_sales=build_repeat_sales_rows(ctx),
        comparable_units=build_comparable_unit_rows(ctx),
        turnover=build_turnover_rows(ctx),
        annual_ownership_residency=build_annual_ownership_residency_rows(ctx),
        price_vs_residency=build_price_vs_residency_rows(ctx),
        statistics=build_statistics_rows(ctx, monthly_market),
        counterfactual=counterfactual,
    )


def write_study_csvs(output_dir: Path, outputs: StudyOutputs) -> None:
    write_csv(output_dir / "bill9-event-study-summary.csv", SUMMARY_COLUMNS, outputs.summary)
    write_csv(output_dir / "bill9-monthly-market.csv", MONTHLY_MARKET_COLUMNS, outputs.monthly_market)
    write_csv(
        output_dir / "bill9-monthly-residency.csv",
        MONTHLY_RESIDENCY_COLUMNS,
        outputs.monthly_residency,
    )
    write_csv(
        output_dir / "bill9-price-distribution.csv",
        PRICE_DISTRIBUTION_COLUMNS,
        outputs.price_distribution,
    )
    write_csv(output_dir / "bill9-repeat-sales.csv", REPEAT_SALES_COLUMNS, outputs.repeat_sales)
    write_csv(
        output_dir / "bill9-comparable-units.csv",
        COMPARABLE_UNITS_COLUMNS,
        outputs.comparable_units,
    )
    write_csv(output_dir / "bill9-turnover.csv", TURNOVER_COLUMNS, outputs.turnover)
    write_csv(output_dir / "bill9-statistics.csv", STATISTICS_COLUMNS, outputs.statistics)
    write_csv(
        output_dir / "bill9-counterfactual.csv",
        COUNTERFACTUAL_COLUMNS,
        outputs.counterfactual.rows,
    )
