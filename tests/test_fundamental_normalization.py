from datetime import datetime, timezone

import pytest

from bistbot.fundamental.models import FinancialPeriod, Metric
from bistbot.fundamental.normalization import yoy_growth


def _period(year: int, month: int, day: int, revenue: float) -> FinancialPeriod:
    return FinancialPeriod(
        period_end=datetime(year, month, day, tzinfo=timezone.utc),
        revenue=Metric(value=revenue, available=True, source="fixture"),
    )


def test_yoy_growth_matches_calendar_quarter_when_intervening_quarters_are_missing():
    periods = [
        _period(2026, 6, 30, 868_737_182),
        _period(2026, 3, 31, 55_813_306),
        _period(2025, 12, 31, 167_964_131),
        _period(2025, 6, 30, 716_607_405),
        _period(2025, 3, 31, 13_235_139),
        _period(2024, 12, 31, 888_774_942),
    ]

    assert yoy_growth(periods, "revenue") == pytest.approx(21.2291617)


def test_yoy_growth_is_unavailable_without_a_corresponding_prior_year_quarter():
    periods = [
        _period(2026, 6, 30, 868_737_182),
        _period(2025, 3, 31, 13_235_139),
    ]

    assert yoy_growth(periods, "revenue") is None
