from __future__ import annotations

from datetime import date
from pathlib import Path

from maui_market.bill9_event_study import (
    BILL9_DISCLAIMER,
    LIMITATIONS_TEXT,
    StudyContext,
    StudyOutputs,
)
from maui_market.bill9_policy import PolicyMilestone

COUNTERFACTUAL_LIMITATIONS = """\
- Counterfactual forecasts extrapolate Maui Kamaole's pre-announcement price trend and assume it would have continued absent Bill 9 or other shocks.
- Pre-announcement data include the 2021–2022 price spike; linear and log-linear models respond differently to that volatility.
- Months with few sales leave observed medians missing; the expected path is model-based.
- Difference-in-differences results require comparable control complexes with prepared county sales data.
- Counterfactual gaps are descriptive and do not establish that Bill 9 caused the deviation.
"""


def _summary_value(outputs: StudyOutputs, metric: str) -> str:
    for row in outputs.summary:
        if row["metric"] == metric:
            return row["value"]
    return "n/a"


def _money(value: str) -> str:
    if not value or value == "n/a":
        return "n/a"
    try:
        amount = float(value)
    except ValueError:
        return value
    return f"${amount:,.0f}"


def _chart_path(chart_paths: list[Path], name: str) -> str:
    for path in chart_paths:
        if path.name == name:
            return f"charts/{path.name}"
    return ""


def _milestone_lines(milestones: tuple[PolicyMilestone, ...]) -> list[str]:
    lines = ["| Date | Event |", "|------|-------|"]
    for milestone in milestones:
        lines.append(f"| {milestone.date.isoformat()} | {milestone.label} |")
    return lines


def build_markdown_report(
    ctx: StudyContext,
    outputs: StudyOutputs,
    chart_paths: list[Path],
    *,
    output_path: Path,
) -> str:
    cf = outputs.counterfactual
    lines = [
        "# Market Response to Maui County Bill 9 — Maui Kamaole Event Study (2019–Present)",
        "",
        f"*Generated {date.today().isoformat()}*",
        "",
        "> **Interpretation notice:** " + BILL9_DISCLAIMER,
        "",
        "## 1. Executive Summary",
        "",
        f"Maui Kamaole ({_summary_value(outputs, 'tmks')}) comprises "
        f"{_summary_value(outputs, 'total_units')} condominium units. "
        f"From {_summary_value(outputs, 'study_period_start')} through "
        f"{_summary_value(outputs, 'study_period_end')}, county records show "
        f"**{_summary_value(outputs, 'total_transfers')} transfers** and "
        f"**{_summary_value(outputs, 'arms_length_sales')} arm's-length sales**.",
        "",
        f"- Median sale price (study period): {_money(_summary_value(outputs, 'median_sale_price'))}",
        f"- Portfolio ownership (proxy): {_summary_value(outputs, 'portfolio_hi_pct')}% Hawaii / "
        f"{_summary_value(outputs, 'portfolio_non_hi_pct')}% non-Hawaii",
        f"- Transfers attributed to non-Hawaii owners (dominant proxy): "
        f"{_summary_value(outputs, 'transfer_non_hi_pct')}%",
        "",
        _counterfactual_headline_paragraph(outputs),
        "",
        "### Policy timeline",
        "",
        *_milestone_lines(ctx.policy.milestones),
        "",
        "## 2. Counterfactual Market Value Analysis (Section 15)",
        "",
        "A simple pre/post median comparison can understate deviation from the market trajectory "
        "owners might have expected. This section compares **observed** sale prices to a "
        "**counterfactual** forecast fitted on pre-announcement data (January 2019 through April 2024).",
        "",
        _counterfactual_detail_section(outputs),
        "",
    ]

    its_chart = _chart_path(chart_paths, "01_actual_vs_counterfactual.png")
    if its_chart:
        lines.extend(["", f"![Actual vs counterfactual]({its_chart})", ""])

    lines.extend(
        [
            _sensitivity_section(cf),
            "",
            _did_section(cf),
            "",
            "> Counterfactual estimates are **descriptive**. They measure deviation from a "
            "pre-announcement trend, not proof that Bill 9 caused the gap.",
            "",
            "## 3. Market Activity",
            "",
            "Monthly transfers, arm's-length sales, and price indices are in `bill9-monthly-market.csv`.",
            "",
        ]
    )

    median_chart = _chart_path(chart_paths, "02_monthly_median_price.png")
    volume_chart = _chart_path(chart_paths, "03_transaction_volume.png")
    index_chart = _chart_path(chart_paths, "05_indexed_market_value.png")
    if median_chart:
        lines.extend(["", f"![Monthly median sale price]({median_chart})", ""])
    if volume_chart:
        lines.extend(["", f"![Monthly transaction volume]({volume_chart})", ""])
    if index_chart:
        lines.extend(["", f"![Indexed market value]({index_chart})", ""])

    lines.extend(
        [
            "",
            "## 4. Ownership & Residency",
            "",
            _residency_summary(outputs),
            "",
            "Buyer residency among transfers uses the current owner mailing-address proxy. "
            "See `bill9-monthly-residency.csv`.",
            "",
        ]
    )

    ownership_chart = _chart_path(chart_paths, "04_ownership_residency.png")
    if ownership_chart:
        lines.extend(["", f"![Ownership residency]({ownership_chart})", ""])

    lines.extend(
        [
            "",
            "## 5. Supporting Evidence",
            "",
            _supporting_evidence_section(outputs),
            "",
            "## 6. Limitations",
            "",
            LIMITATIONS_TEXT,
            "",
            COUNTERFACTUAL_LIMITATIONS,
            "",
            "## 7. Data Files",
            "",
            "- `bill9-event-study-summary.csv` — top-line and counterfactual headline metrics",
            "- `bill9-counterfactual.csv` — monthly expected vs observed medians (linear & log-linear)",
            "- `bill9-monthly-market.csv`",
            "- `bill9-monthly-residency.csv`",
            "- `bill9-repeat-sales.csv`",
            "- `bill9-comparable-units.csv`",
            "- `bill9-turnover.csv`",
            "- `bill9-price-distribution.csv`",
            "- `bill9-statistics.csv`",
            "",
        ]
    )

    did_chart = _chart_path(chart_paths, "06_did_comparison.png")
    if did_chart:
        insert_at = lines.index("## 3. Market Activity")
        lines[insert_at:insert_at] = ["", f"![DiD comparison]({did_chart})", ""]

    text = "\n".join(lines)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return text


def _counterfactual_headline_paragraph(outputs: StudyOutputs) -> str:
    month = _summary_value(outputs, "counterfactual_month")
    expected = _money(_summary_value(outputs, "counterfactual_expected_median"))
    observed = _money(_summary_value(outputs, "counterfactual_observed_median"))
    gap_raw = _summary_value(outputs, "counterfactual_gap_dollars")
    gap_pct = _summary_value(outputs, "counterfactual_gap_pct")
    portfolio_raw = _summary_value(outputs, "counterfactual_portfolio_impact")
    if month == "n/a":
        return (
            "_Counterfactual analysis unavailable — insufficient pre-announcement "
            "rolling median price data._"
        )
    try:
        gap_value = float(gap_raw)
        shortfall = _money(str(abs(gap_value)))
        direction = "below" if gap_value < 0 else "above"
        portfolio = _money(str(abs(float(portfolio_raw)))) if portfolio_raw not in ("", "n/a") else "n/a"
    except ValueError:
        shortfall = _money(gap_raw)
        direction = "versus"
        portfolio = _money(portfolio_raw)
    pct_text = ""
    if gap_pct not in ("", "n/a"):
        try:
            pct_text = f" ({abs(float(gap_pct)):.1f}% {direction} expected)"
        except ValueError:
            pct_text = f" ({gap_pct}%)"
    return (
        f"**Counterfactual (log-linear trend):** Based on Maui Kamaole's pre-announcement "
        f"appreciation trend, the median unit would have been expected to sell for approximately "
        f"**{expected}** by {month}. The observed rolling median of **{observed}** is "
        f"**{shortfall}** {direction} that forecast{pct_text}, equivalent to approximately "
        f"**{portfolio}** across the complex. This assumes pre-announcement trends would "
        f"otherwise have continued; the 2021–2022 price spike makes log-linear extrapolation "
        f"sensitive — see linear model and fixed-rate sensitivity below."
    )


def _counterfactual_detail_section(outputs: StudyOutputs) -> str:
    linear = _money(_summary_value(outputs, "counterfactual_linear_expected_median"))
    cagr = _summary_value(outputs, "counterfactual_cagr_pre_announcement")
    lines = [
        "| Model | Expected median | Notes |",
        "|-------|-----------------|-------|",
        (
            f"| Log-linear (primary) | {_money(_summary_value(outputs, 'counterfactual_expected_median'))} | "
            f"Implied pre-period CAGR: {cagr}%/yr |"
        ),
        f"| Linear | {linear} | Dollar-per-month trend |",
    ]
    return "\n".join(lines)


def _sensitivity_section(cf) -> str:
    if not cf.sensitivity_rows:
        return ""
    lines = [
        "### Fixed-rate sensitivity (illustrative)",
        "",
        "If pre-announcement median had compounded at fixed annual rates from the last "
        "pre-announcement month:",
        "",
        "| Annual rate | Years | Expected median |",
        "|-------------|-------|-----------------|",
    ]
    for row in cf.sensitivity_rows:
        lines.append(
            f"| {row['annual_rate_pct']}% | {row['years']} | {_money(row['expected_median'])} |"
        )
    return "\n".join(lines)


def _did_section(cf) -> str:
    if not cf.did_available:
        return (
            "_Difference-in-differences not computed — no control complexes loaded. "
            "Run with `--controls` after preparing county sales data for comparable complexes._"
        )
    pct = f"{cf.did_estimate_pct:.2f}%" if cf.did_estimate_pct is not None else "n/a"
    return (
        f"**Difference-in-differences:** Treatment log-price change minus pooled controls "
        f"({cf.did_control_labels}) implies an approximate **{pct}** differential on price "
        f"levels after the announcement. See `bill9-counterfactual.csv` model=`did`."
    )


def _residency_summary(outputs: StudyOutputs) -> str:
    if not outputs.annual_ownership_residency:
        return "_No annual residency data._"
    first = outputs.annual_ownership_residency[0]
    last = outputs.annual_ownership_residency[-1]
    return (
        f"Portfolio non-Hawaii ownership was {first['portfolio_non_hi_pct']}% in {first['year']} "
        f"and {last['portfolio_non_hi_pct']}% in {last['year']} (year-end proxy). "
        f"Non-Hawaii share among transfers: {first['transfer_non_hi_pct']}% → "
        f"{last['transfer_non_hi_pct']}%."
    )


def _supporting_evidence_section(outputs: StudyOutputs) -> str:
    parts: list[str] = []

    if outputs.price_distribution:
        pre = next(
            (row for row in outputs.price_distribution if "Before" in row["cohort"]),
            None,
        )
        post = next(
            (row for row in outputs.price_distribution if "After announcement" in row["cohort"]),
            None,
        )
        if pre and post:
            parts.append(
                f"**Price distribution:** Pre-announcement median {_money(pre['median'])} "
                f"({pre['sale_count']} sales) vs post-announcement through passage "
                f"{_money(post['median'])} ({post['sale_count']} sales). "
                f"See `bill9-price-distribution.csv`."
            )

    parts.append(
        f"**Repeat sales:** {len(outputs.repeat_sales)} consecutive arm's-length pairs in "
        "`bill9-repeat-sales.csv`."
    )

    annual_turnover = [row for row in outputs.turnover if row["period_type"] == "annual"]
    if annual_turnover:
        latest = annual_turnover[-1]
        parts.append(
            f"**Turnover:** {latest['year']} turnover rate {latest['turnover_rate']}% "
            f"({latest['unique_units_transferred']} unique units). See `bill9-turnover.csv`."
        )

    if outputs.statistics:
        highlights = []
        for row in outputs.statistics:
            if row["test_name"] == "Mann-Whitney U":
                highlights.append(
                    f"{row['variable']}: {row['interpretation']} (p={row['p_value'] or 'n/a'})"
                )
        if highlights:
            parts.append(
                "**Statistical tests (Mann-Whitney, pre- vs post-announcement):** "
                + "; ".join(highlights[:3])
                + ". See `bill9-statistics.csv`."
            )

    return "\n\n".join(parts)
