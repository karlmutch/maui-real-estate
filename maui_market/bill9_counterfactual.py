from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

import numpy as np
from scipy import stats

if TYPE_CHECKING:
    from maui_market.bill9_event_study import StudyContext

COUNTERFACTUAL_COLUMNS = [
    "month",
    "model",
    "observed_median",
    "expected_median",
    "ci_low",
    "ci_high",
    "gap_dollars",
    "gap_pct",
    "portfolio_gap_dollars",
    "is_post_announcement",
    "notes",
]

SENSITIVITY_RATES = (0.03, 0.05, 0.07, 0.08)


@dataclass
class TrendModel:
    name: str
    slope: float
    intercept: float
    residual_std: float
    n: int
    x_mean: float
    ss_x: float
    log_space: bool

    @property
    def monthly_growth_rate(self) -> float:
        if self.log_space:
            return self.slope
        return 0.0

    @property
    def annualized_cagr(self) -> float | None:
        if not self.log_space or self.slope <= -1:
            return None
        return (math.exp(self.slope * 12) - 1) * 100.0


@dataclass
class CounterfactualOutputs:
    rows: list[dict[str, str]] = field(default_factory=list)
    headline: dict[str, str] = field(default_factory=dict)
    sensitivity_rows: list[dict[str, str]] = field(default_factory=list)
    did_available: bool = False
    did_estimate_log: float | None = None
    did_estimate_pct: float | None = None
    did_control_labels: str = ""
    treatment_indexed: list[tuple[str, float]] = field(default_factory=list)
    control_indexed: list[tuple[str, float]] = field(default_factory=list)


def _parse_float(value: str) -> float | None:
    if not value:
        return None
    return float(value)


def month_index(month: str, origin: date) -> int:
    year, month_num = map(int, month.split("-"))
    return (year - origin.year) * 12 + (month_num - origin.month)


def announcement_month(policy) -> str:
    milestone = next((item for item in policy.milestones if item.id == "announcement"), None)
    if milestone is None:
        return ""
    return milestone.date.strftime("%Y-%m")


def rolling_series(monthly_market: list[dict[str, str]]) -> list[tuple[str, float | None]]:
    return [
        (row["month"], _parse_float(row["rolling_3mo_median_price"]))
        for row in monthly_market
    ]


def fit_trend(x_values: list[float], y_values: list[float], *, name: str, log_space: bool) -> TrendModel:
    x = np.array(x_values, dtype=float)
    y_raw = np.array(y_values, dtype=float)
    y = np.log(y_raw) if log_space else y_raw
    result = stats.linregress(x, y)
    y_hat = result.intercept + result.slope * x
    residuals = y - y_hat
    df = max(len(x) - 2, 1)
    residual_std = float(np.sqrt(np.sum(residuals**2) / df))
    ss_x = float(np.sum((x - float(np.mean(x))) ** 2))
    return TrendModel(
        name=name,
        slope=float(result.slope),
        intercept=float(result.intercept),
        residual_std=residual_std,
        n=len(x),
        x_mean=float(np.mean(x)),
        ss_x=ss_x,
        log_space=log_space,
    )


def predict(model: TrendModel, x: float) -> float:
    predicted = model.intercept + model.slope * x
    if model.log_space:
        return float(math.exp(predicted))
    return predicted


def prediction_interval(model: TrendModel, x: float, *, alpha: float = 0.05) -> tuple[float, float, float]:
    t_crit = float(stats.t.ppf(1 - alpha / 2, max(model.n - 2, 1)))
    if model.ss_x <= 0:
        se_pred = model.residual_std
    else:
        se_pred = model.residual_std * math.sqrt(
            1 + 1 / model.n + (x - model.x_mean) ** 2 / model.ss_x
        )
    center = model.intercept + model.slope * x
    low = center - t_crit * se_pred
    high = center + t_crit * se_pred
    if model.log_space:
        return float(math.exp(center)), float(math.exp(low)), float(math.exp(high))
    return center, low, high


def _format_optional(value: float | None, *, digits: int = 0) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def _format_pct(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.4f}"


def _gap_fields(
    observed: float | None,
    expected: float,
    total_units: int,
) -> tuple[str, str, str]:
    if observed is None or expected == 0:
        return "", "", ""
    gap = observed - expected
    gap_pct = 100.0 * gap / expected
    portfolio_gap = gap * total_units
    return (
        _format_optional(gap, digits=0),
        _format_pct(gap_pct),
        _format_optional(portfolio_gap, digits=0),
    )


def build_model_rows(
    *,
    model: TrendModel,
    series: list[tuple[str, float | None]],
    origin: date,
    announcement: str,
    total_units: int,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for month, observed in series:
        x = month_index(month, origin)
        expected, ci_low, ci_high = prediction_interval(model, float(x))
        is_post = "yes" if month >= announcement else "no"
        gap_dollars, gap_pct, portfolio_gap = _gap_fields(observed, expected, total_units)
        rows.append(
            {
                "month": month,
                "model": model.name,
                "observed_median": _format_optional(observed, digits=0),
                "expected_median": _format_optional(expected, digits=0),
                "ci_low": _format_optional(ci_low, digits=0),
                "ci_high": _format_optional(ci_high, digits=0),
                "gap_dollars": gap_dollars,
                "gap_pct": gap_pct,
                "portfolio_gap_dollars": portfolio_gap,
                "is_post_announcement": is_post,
                "notes": (
                    "3-month rolling median; log-linear CAGR pre-announcement"
                    if model.log_space
                    else "3-month rolling median; linear trend pre-announcement"
                ),
            }
        )
    return rows


def build_sensitivity_rows(
    *,
    baseline_price: float,
    baseline_month: str,
    latest_month: str,
    origin: date,
) -> list[dict[str, str]]:
    years = (month_index(latest_month, origin) - month_index(baseline_month, origin)) / 12.0
    if years <= 0:
        return []
    rows: list[dict[str, str]] = []
    for rate in SENSITIVITY_RATES:
        expected = baseline_price * ((1 + rate) ** years)
        rows.append(
            {
                "annual_rate_pct": _format_pct(rate * 100),
                "years": _format_optional(years, digits=2),
                "expected_median": _format_optional(expected, digits=0),
                "notes": f"compound growth from {baseline_month} pre-announcement baseline",
            }
        )
    return rows


def _mean_log_price(months: list[str], series: dict[str, float | None], cutoff: str, *, pre: bool) -> float | None:
    values: list[float] = []
    for month in months:
        if pre and month >= cutoff:
            continue
        if not pre and month < cutoff:
            continue
        price = series.get(month)
        if price is not None and price > 0:
            values.append(math.log(price))
    if not values:
        return None
    return float(np.mean(values))


def build_indexed_series(
    series: list[tuple[str, float | None]],
    *,
    baseline_month: str = "2019-01",
) -> list[tuple[str, float]]:
    baseline = next((value for month, value in series if month == baseline_month and value), None)
    if baseline is None:
        for _month, value in series:
            if value is not None:
                baseline = value
                break
    if baseline is None or baseline <= 0:
        return []
    indexed: list[tuple[str, float]] = []
    for month, value in series:
        if value is not None:
            indexed.append((month, 100.0 * value / baseline))
    return indexed


def build_counterfactual(
    ctx: StudyContext,
    monthly_market: list[dict[str, str]],
    control_contexts: list[StudyContext] | None = None,
) -> CounterfactualOutputs:
    origin = ctx.policy.analysis_start
    announcement = announcement_month(ctx.policy)
    series = rolling_series(monthly_market)
    series_map = {month: value for month, value in series}

    pre_points = [
        (month_index(month, origin), value)
        for month, value in series
        if value is not None and month < announcement
    ]
    if len(pre_points) < 3:
        return CounterfactualOutputs()

    x_pre = [float(point[0]) for point in pre_points]
    y_pre = [float(point[1]) for point in pre_points]

    linear_model = fit_trend(x_pre, y_pre, name="linear", log_space=False)
    log_model = fit_trend(x_pre, y_pre, name="log_linear", log_space=True)

    rows = build_model_rows(
        model=linear_model,
        series=series,
        origin=origin,
        announcement=announcement,
        total_units=ctx.total_units,
    )
    rows.extend(
        build_model_rows(
            model=log_model,
            series=series,
            origin=origin,
            announcement=announcement,
            total_units=ctx.total_units,
        )
    )

    headline = _build_headline(rows, log_model, announcement)
    latest_month = monthly_market[-1]["month"] if monthly_market else ""
    pre_end_month = max(month for month, value in series if value is not None and month < announcement)
    pre_end_price = series_map.get(pre_end_month)
    sensitivity_rows: list[dict[str, str]] = []
    if pre_end_price is not None and latest_month:
        sensitivity_rows = build_sensitivity_rows(
            baseline_price=pre_end_price,
            baseline_month=pre_end_month,
            latest_month=latest_month,
            origin=origin,
        )

    outputs = CounterfactualOutputs(
        rows=rows,
        headline=headline,
        sensitivity_rows=sensitivity_rows,
        treatment_indexed=build_indexed_series(series),
    )

    if not control_contexts:
        return outputs

    months = [row["month"] for row in monthly_market]
    treatment_pre = _mean_log_price(months, series_map, announcement, pre=True)
    treatment_post = _mean_log_price(months, series_map, announcement, pre=False)
    if treatment_pre is None or treatment_post is None:
        return outputs

    control_labels: list[str] = []
    control_pre_post_deltas: list[float] = []
    pooled_control: dict[str, list[float]] = {}

    for control in control_contexts:
        if not control.events:
            continue
        from maui_market.bill9_event_study import build_monthly_market_rows

        control_market = build_monthly_market_rows(control)
        control_series = rolling_series(control_market)
        control_map = {month: value for month, value in control_series}
        control_pre = _mean_log_price(months, control_map, announcement, pre=True)
        control_post = _mean_log_price(months, control_map, announcement, pre=False)
        if control_pre is None or control_post is None:
            continue
        control_labels.append(control.combined_tmks)
        control_pre_post_deltas.append(control_post - control_pre)
        for month, value in control_series:
            if value is not None:
                pooled_control.setdefault(month, []).append(value)

    if not control_pre_post_deltas:
        return outputs

    avg_control_delta = float(np.mean(control_pre_post_deltas))
    treatment_delta = treatment_post - treatment_pre
    did_log = treatment_delta - avg_control_delta
    outputs.did_available = True
    outputs.did_estimate_log = did_log
    outputs.did_estimate_pct = (math.exp(did_log) - 1) * 100.0
    outputs.did_control_labels = ", ".join(control_labels)

    pooled_map: dict[str, float | None] = {}
    for month, values in pooled_control.items():
        pooled_map[month] = float(np.median(values)) if values else None
    pooled_series = [(month, pooled_map.get(month)) for month in months]
    outputs.control_indexed = build_indexed_series(pooled_series)

    rows.append(
        {
            "month": latest_month,
            "model": "did",
            "observed_median": headline.get("counterfactual_observed_median", ""),
            "expected_median": "",
            "ci_low": "",
            "ci_high": "",
            "gap_dollars": "",
            "gap_pct": _format_pct(outputs.did_estimate_pct),
            "portfolio_gap_dollars": "",
            "is_post_announcement": "yes",
            "notes": (
                f"DiD log-price change vs controls ({outputs.did_control_labels}); "
                "approximate % effect on levels"
            ),
        }
    )
    outputs.rows = rows
    return outputs


def _build_headline(rows: list[dict[str, str]], log_model: TrendModel, announcement: str) -> dict[str, str]:
    log_rows = [row for row in rows if row["model"] == "log_linear" and row["is_post_announcement"] == "yes"]
    candidate = None
    for row in reversed(log_rows):
        if row["observed_median"] and row["expected_median"]:
            candidate = row
            break
    if candidate is None:
        for row in reversed(log_rows):
            if row["expected_median"]:
                candidate = row
                break
    if candidate is None:
        return {}

    linear_row = next(
        (
            row
            for row in rows
            if row["model"] == "linear"
            and row["month"] == candidate["month"]
        ),
        None,
    )
    cagr = log_model.annualized_cagr
    return {
        "counterfactual_month": candidate["month"],
        "counterfactual_expected_median": candidate["expected_median"],
        "counterfactual_observed_median": candidate["observed_median"],
        "counterfactual_gap_dollars": candidate["gap_dollars"],
        "counterfactual_gap_pct": candidate["gap_pct"],
        "counterfactual_portfolio_impact": candidate["portfolio_gap_dollars"],
        "counterfactual_model": "log_linear",
        "counterfactual_linear_expected_median": (
            linear_row["expected_median"] if linear_row else ""
        ),
        "counterfactual_cagr_pre_announcement": _format_pct(cagr) if cagr is not None else "",
        "counterfactual_announcement_month": announcement,
    }


def counterfactual_summary_rows(headline: dict[str, str]) -> list[dict[str, str]]:
    if not headline:
        return []
    mapping = {
        "counterfactual_month": "latest month with counterfactual comparison",
        "counterfactual_expected_median": "log-linear forecast median (primary)",
        "counterfactual_observed_median": "observed 3-month rolling median",
        "counterfactual_gap_dollars": "observed minus expected per unit",
        "counterfactual_gap_pct": "gap as percent of expected",
        "counterfactual_portfolio_impact": "gap × total units",
        "counterfactual_model": "primary counterfactual model",
        "counterfactual_linear_expected_median": "linear trend forecast at same month",
        "counterfactual_cagr_pre_announcement": "implied annual CAGR from log-linear pre-period fit",
        "counterfactual_announcement_month": "Bill 9 announcement month",
    }
    return [
        {"metric": metric, "value": value, "notes": mapping.get(metric, "")}
        for metric, value in headline.items()
        if value
    ]
