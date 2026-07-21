from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "bill9_policy.yaml"


@dataclass(frozen=True)
class PolicyMilestone:
    id: str
    date: date
    label: str


@dataclass(frozen=True)
class PolicyWindow:
    id: str
    start: date
    end: date | None


@dataclass(frozen=True)
class Bill9Policy:
    analysis_start: date
    milestones: tuple[PolicyMilestone, ...]
    windows: tuple[PolicyWindow, ...]
    interest_rate_periods: tuple[tuple[date, date], ...]


def _parse_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def load_bill9_policy(path: Path | None = None) -> Bill9Policy:
    config_path = path or CONFIG_PATH
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    milestones = tuple(
        PolicyMilestone(
            id=item["id"],
            date=_parse_date(item["date"]),  # type: ignore[arg-type]
            label=item["label"],
        )
        for item in data.get("milestones", [])
    )

    windows = tuple(
        PolicyWindow(
            id=window_id,
            start=_parse_date(window["start"]),  # type: ignore[arg-type]
            end=_parse_date(window.get("end")),
        )
        for window_id, window in data.get("windows", {}).items()
    )

    interest_rate_periods = tuple(
        (_parse_date(period["start"]), _parse_date(period["end"]))  # type: ignore[misc]
        for period in data.get("interest_rate_periods", [])
        if period.get("start") and period.get("end")
    )

    return Bill9Policy(
        analysis_start=_parse_date(data["analysis_start"]),  # type: ignore[arg-type]
        milestones=milestones,
        windows=windows,
        interest_rate_periods=interest_rate_periods,
    )


def window_end(window: PolicyWindow, *, today: date | None = None) -> date:
    if window.end is not None:
        return window.end
    return today or date.today()


def date_in_window(event_date: date, window: PolicyWindow, *, today: date | None = None) -> bool:
    end = window_end(window, today=today)
    return window.start <= event_date <= end


def era_for_date(event_date: date, policy: Bill9Policy, *, today: date | None = None) -> str:
    for window in policy.windows:
        if date_in_window(event_date, window, today=today):
            return window.id
    return "outside_study"
