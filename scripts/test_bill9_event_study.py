#!/usr/bin/env python3
"""Tests for Bill 9 event study analysis."""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from maui_market.bill9_event_study import (  # noqa: E402
    EnrichedEvent,
    StudyContext,
    build_monthly_market_rows,
    build_repeat_sales_rows,
    dominant_residency,
    era_for_date,
    parse_unit_building,
    run_analysis,
)
from maui_market.bill9_counterfactual import (  # noqa: E402
    build_counterfactual,
    fit_trend,
    predict,
)
from maui_market.bill9_policy import (  # noqa: E402
    Bill9Policy,
    PolicyMilestone,
    PolicyWindow,
    load_bill9_policy,
)


def make_event(
    *,
    parid: str = "390040820001",
    sale_date: date = date(2020, 6, 15),
    price: float | None = 800_000.0,
    hi_pct: float = 10.0,
    non_hi_pct: float = 90.0,
) -> EnrichedEvent:
    return EnrichedEvent(
        tmk="239004082",
        parid=parid,
        cpr="0001",
        unit="G 101",
        building="G",
        sale_date=sale_date,
        year=sale_date.year,
        month=sale_date.strftime("%Y-%m"),
        price=price,
        building_value_bucket="250000",
        hi_pct=hi_pct,
        non_hi_pct=non_hi_pct,
        unknown_pct=0.0,
    )


def make_policy() -> Bill9Policy:
    return Bill9Policy(
        analysis_start=date(2019, 1, 1),
        milestones=(
            PolicyMilestone("baseline", date(2019, 1, 1), "Baseline"),
            PolicyMilestone("announcement", date(2024, 5, 1), "Announced"),
            PolicyMilestone("passage", date(2025, 12, 15), "Passed"),
        ),
        windows=(
            PolicyWindow("pre_announcement", date(2019, 1, 1), date(2024, 4, 30)),
            PolicyWindow(
                "post_announcement_pre_passage",
                date(2024, 5, 1),
                date(2025, 12, 14),
            ),
            PolicyWindow("post_passage", date(2025, 12, 15), None),
        ),
        interest_rate_periods=(),
    )


def make_context(events: list[EnrichedEvent]) -> StudyContext:
    return StudyContext(
        policy=make_policy(),
        combined_tmks="239004082",
        total_units=10,
        units=[],
        units_by_parid={},
        event_dates_by_parid={},
        events=events,
        today=date(2026, 7, 12),
    )


class Bill9PolicyTests(unittest.TestCase):
    def test_load_default_policy(self) -> None:
        policy = load_bill9_policy()
        self.assertEqual(policy.analysis_start, date(2019, 1, 1))
        self.assertTrue(any(item.id == "announcement" for item in policy.milestones))


class Bill9HelperTests(unittest.TestCase):
    def test_parse_unit_building(self) -> None:
        self.assertEqual(parse_unit_building("G 101"), "G")
        self.assertEqual(parse_unit_building("H-201"), "H")

    def test_dominant_residency(self) -> None:
        self.assertEqual(dominant_residency(80, 10, 10), "hi")
        self.assertEqual(dominant_residency(10, 80, 10), "non_hi")
        self.assertEqual(dominant_residency(10, 10, 80), "unknown")

    def test_era_for_date(self) -> None:
        policy = make_policy()
        self.assertEqual(
            era_for_date(date(2020, 1, 1), policy, today=date(2026, 1, 1)),
            "pre_announcement",
        )
        self.assertEqual(
            era_for_date(date(2024, 8, 1), policy, today=date(2026, 1, 1)),
            "post_announcement_pre_passage",
        )


class Bill9AnalysisTests(unittest.TestCase):
    def test_monthly_market_aggregation(self) -> None:
        events = [
            make_event(sale_date=date(2020, 1, 10), price=700_000),
            make_event(parid="390040820002", sale_date=date(2020, 1, 20), price=900_000),
            make_event(parid="390040820003", sale_date=date(2020, 2, 5), price=800_000),
        ]
        ctx = make_context(events)
        rows = build_monthly_market_rows(ctx)
        jan = next(row for row in rows if row["month"] == "2020-01")
        self.assertEqual(jan["arms_length_sale_count"], "2")
        self.assertEqual(jan["median_price"], "800000")

    def test_repeat_sales_appreciation(self) -> None:
        events = [
            make_event(sale_date=date(2019, 1, 1), price=500_000),
            make_event(sale_date=date(2021, 1, 1), price=600_000),
        ]
        ctx = make_context(events)
        rows = build_repeat_sales_rows(ctx)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["appreciation_pct"], "20.0000")
        self.assertAlmostEqual(float(rows[0]["annualized_appreciation_pct"]), 9.9932, places=3)

    def test_run_analysis_produces_statistics(self) -> None:
        events = [
            make_event(sale_date=date(2020, 1, 1), price=700_000),
            make_event(parid="2", sale_date=date(2020, 2, 1), price=750_000),
            make_event(parid="3", sale_date=date(2020, 3, 1), price=800_000),
            make_event(parid="4", sale_date=date(2025, 6, 1), price=650_000),
            make_event(parid="5", sale_date=date(2025, 7, 1), price=700_000),
        ]
        ctx = make_context(events)
        outputs = run_analysis(ctx)
        self.assertTrue(outputs.monthly_market)
        self.assertTrue(outputs.statistics)
        self.assertTrue(any(row["test_name"] == "Mann-Whitney U" for row in outputs.statistics))


class Bill9CounterfactualTests(unittest.TestCase):
    def test_log_linear_trend_predicts_growth(self) -> None:
        x = [0.0, 12.0, 24.0, 36.0, 48.0, 60.0]
        y = [600_000, 630_000, 660_000, 690_000, 720_000, 750_000]
        model = fit_trend(x, y, name="log_linear", log_space=True)
        self.assertGreater(predict(model, 72.0), 750_000)

    def test_counterfactual_without_controls(self) -> None:
        events: list[EnrichedEvent] = []
        price = 600_000.0
        for index in range(64):
            year = 2019 + index // 12
            month = index % 12 + 1
            events.append(
                make_event(
                    parid=f"pre-{index}",
                    sale_date=date(year, month, 10),
                    price=price,
                )
            )
            price += 4_000
        for index in range(6):
            events.append(
                make_event(
                    parid=f"post-{index}",
                    sale_date=date(2024, 5 + index, 10),
                    price=700_000.0,
                )
            )
        ctx = make_context(events)
        monthly = build_monthly_market_rows(ctx)
        cf = build_counterfactual(ctx, monthly)
        self.assertTrue(cf.rows)
        self.assertTrue(cf.headline.get("counterfactual_expected_median"))
        outputs = run_analysis(ctx)
        metrics = [row["metric"] for row in outputs.summary]
        self.assertIn("counterfactual_expected_median", metrics)
        self.assertFalse(outputs.counterfactual.did_available)


if __name__ == "__main__":
    unittest.main()
