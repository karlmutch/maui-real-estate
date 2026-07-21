from __future__ import annotations

from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from maui_market.bill9_event_study import StudyContext, StudyOutputs
from maui_market.bill9_policy import PolicyMilestone

CHART_DPI = 150
CHART_SIZE = (10, 5)
COLORS = {
    "hi": "#2ca02c",
    "non_hi": "#ff7f0e",
    "price": "#1f77b4",
    "expected": "#9467bd",
    "expected_linear": "#8c564b",
    "volume": "#17becf",
    "control": "#d62728",
}


def add_policy_milestones(
    ax: plt.Axes,
    milestones: tuple[PolicyMilestone, ...],
    *,
    ymin: float | None = None,
    ymax: float | None = None,
) -> None:
    if ymin is None or ymax is None:
        ymin, ymax = ax.get_ylim()
    for milestone in milestones:
        if milestone.id == "baseline":
            continue
        x = datetime.combine(milestone.date, datetime.min.time())
        ax.axvline(x, color="#444444", linestyle="--", linewidth=1, alpha=0.8)
        ax.text(
            x,
            ymax,
            milestone.label,
            rotation=90,
            va="top",
            ha="right",
            fontsize=7,
            color="#333333",
        )


def _parse_month(month: str) -> datetime:
    return datetime.strptime(month + "-01", "%Y-%m-%d")


def _parse_float(value: str) -> float | None:
    if not value:
        return None
    return float(value)


def _series_by_model(outputs: StudyOutputs, model: str) -> dict[str, dict[str, float | None]]:
    result: dict[str, dict[str, float | None]] = {}
    for row in outputs.counterfactual.rows:
        if row["model"] != model:
            continue
        result[row["month"]] = {
            "observed": _parse_float(row["observed_median"]),
            "expected": _parse_float(row["expected_median"]),
            "ci_low": _parse_float(row["ci_low"]),
            "ci_high": _parse_float(row["ci_high"]),
        }
    return result


def generate_bill9_charts(
    ctx: StudyContext,
    outputs: StudyOutputs,
    output_dir: Path,
) -> list[Path]:
    chart_dir = output_dir / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    milestones = ctx.policy.milestones

    months = [row["month"] for row in outputs.monthly_market]
    x = [_parse_month(month) for month in months]

    log_cf = _series_by_model(outputs, "log_linear")
    linear_cf = _series_by_model(outputs, "linear")
    if log_cf:
        observed = [log_cf.get(month, {}).get("observed") for month in months]
        expected_log = [log_cf.get(month, {}).get("expected") for month in months]
        ci_low_arr = np.array(
            [log_cf.get(month, {}).get("ci_low") or np.nan for month in months],
            dtype=float,
        )
        ci_high_arr = np.array(
            [log_cf.get(month, {}).get("ci_high") or np.nan for month in months],
            dtype=float,
        )
        expected_linear = [linear_cf.get(month, {}).get("expected") for month in months]

        fig, ax = plt.subplots(figsize=CHART_SIZE)
        ax.fill_between(
            x,
            ci_low_arr,
            ci_high_arr,
            alpha=0.2,
            color=COLORS["expected"],
            label="Log-linear 95% CI",
        )
        ax.plot(
            x,
            expected_log,
            linestyle="--",
            color=COLORS["expected"],
            linewidth=2,
            label="Expected (log-linear)",
        )
        ax.plot(
            x,
            expected_linear,
            linestyle=":",
            color=COLORS["expected_linear"],
            linewidth=1.5,
            label="Expected (linear)",
        )
        ax.plot(
            x,
            observed,
            marker="o",
            markersize=3,
            color=COLORS["price"],
            label="Observed (3-mo rolling median)",
        )
        add_policy_milestones(ax, milestones)
        ax.set_title("Actual vs counterfactual median price — Maui Kamaole")
        ax.set_xlabel("Month")
        ax.set_ylabel("USD")
        ax.legend(loc="best", fontsize=8)
        fig.autofmt_xdate()
        fig.tight_layout()
        path = chart_dir / "01_actual_vs_counterfactual.png"
        fig.savefig(path, dpi=CHART_DPI)
        plt.close(fig)
        written.append(path)

    median_prices = [_parse_float(row["median_price"]) for row in outputs.monthly_market]
    fig, ax = plt.subplots(figsize=CHART_SIZE)
    ax.plot(x, median_prices, marker="o", markersize=3, color=COLORS["price"], label="Monthly median")
    add_policy_milestones(ax, milestones)
    ax.set_title("Monthly median sale price — Maui Kamaole")
    ax.set_xlabel("Month")
    ax.set_ylabel("USD")
    ax.legend(loc="best")
    fig.autofmt_xdate()
    fig.tight_layout()
    path = chart_dir / "02_monthly_median_price.png"
    fig.savefig(path, dpi=CHART_DPI)
    plt.close(fig)
    written.append(path)

    sale_counts = [
        _parse_float(row["arms_length_sale_count"]) or 0.0 for row in outputs.monthly_market
    ]
    fig, ax = plt.subplots(figsize=CHART_SIZE)
    ax.bar(x, sale_counts, width=20, color=COLORS["volume"], label="Arm's-length sales")
    add_policy_milestones(ax, milestones)
    ax.set_title("Monthly transaction volume (arm's-length sales)")
    ax.set_xlabel("Month")
    ax.set_ylabel("Sales count")
    ax.legend(loc="best")
    fig.autofmt_xdate()
    fig.tight_layout()
    path = chart_dir / "03_transaction_volume.png"
    fig.savefig(path, dpi=CHART_DPI)
    plt.close(fig)
    written.append(path)

    annual = outputs.annual_ownership_residency
    if annual:
        years = [int(row["year"]) for row in annual]
        portfolio_hi = [float(row["portfolio_hi_pct"]) for row in annual]
        portfolio_non_hi = [float(row["portfolio_non_hi_pct"]) for row in annual]
        fig, ax = plt.subplots(figsize=CHART_SIZE)
        ax.plot(years, portfolio_hi, marker="o", color=COLORS["hi"], label="Hawaii ownership %")
        ax.plot(
            years,
            portfolio_non_hi,
            marker="o",
            color=COLORS["non_hi"],
            label="Non-Hawaii ownership %",
        )
        for milestone in milestones:
            if milestone.date.year >= min(years) and milestone.date.year <= max(years):
                ax.axvline(milestone.date.year, color="#444444", linestyle="--", linewidth=1)
        ax.set_title("Ownership residency over time (portfolio proxy)")
        ax.set_xlabel("Year")
        ax.set_ylabel("Percent")
        ax.legend(loc="best")
        fig.tight_layout()
        path = chart_dir / "04_ownership_residency.png"
        fig.savefig(path, dpi=CHART_DPI)
        plt.close(fig)
        written.append(path)

    price_index = [_parse_float(row["price_index"]) for row in outputs.monthly_market]
    fig, ax = plt.subplots(figsize=CHART_SIZE)
    ax.plot(
        x,
        price_index,
        marker="o",
        markersize=3,
        color=COLORS["price"],
        label="Price index (Jan 2019 = 100)",
    )
    add_policy_milestones(ax, milestones)
    ax.set_title("Indexed market value (median sale price)")
    ax.set_xlabel("Month")
    ax.set_ylabel("Index")
    ax.legend(loc="best")
    fig.autofmt_xdate()
    fig.tight_layout()
    path = chart_dir / "05_indexed_market_value.png"
    fig.savefig(path, dpi=CHART_DPI)
    plt.close(fig)
    written.append(path)

    cf = outputs.counterfactual
    if cf.did_available and cf.treatment_indexed and cf.control_indexed:
        treatment_map = dict(cf.treatment_indexed)
        control_map = dict(cf.control_indexed)
        shared_months = [month for month in months if month in treatment_map and month in control_map]
        if shared_months:
            tx = [_parse_month(month) for month in shared_months]
            fig, ax = plt.subplots(figsize=CHART_SIZE)
            ax.plot(
                tx,
                [treatment_map[month] for month in shared_months],
                marker="o",
                markersize=3,
                color=COLORS["price"],
                label="Maui Kamaole (indexed)",
            )
            ax.plot(
                tx,
                [control_map[month] for month in shared_months],
                marker="o",
                markersize=3,
                color=COLORS["control"],
                label="Control complexes (indexed)",
            )
            add_policy_milestones(ax, milestones)
            ax.set_title("Difference-in-differences: indexed rolling median prices")
            ax.set_xlabel("Month")
            ax.set_ylabel("Index (Jan 2019 = 100)")
            ax.legend(loc="best")
            fig.autofmt_xdate()
            fig.tight_layout()
            path = chart_dir / "06_did_comparison.png"
            fig.savefig(path, dpi=CHART_DPI)
            plt.close(fig)
            written.append(path)

    return written
