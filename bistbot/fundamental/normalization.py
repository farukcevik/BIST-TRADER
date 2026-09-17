from __future__ import annotations

from collections.abc import Sequence

from .models import FinancialPeriod


def safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def yoy_growth(periods: Sequence[FinancialPeriod], field: str) -> float | None:
    """Latest quarter against the corresponding quarter one year earlier."""
    if len(periods) < 2:
        return None
    ordered = sorted(periods, key=lambda item: item.period_end, reverse=True)
    latest_period = ordered[0]
    latest_quarter = (latest_period.period_end.month - 1) // 3
    comparable_periods = [
        period
        for period in ordered[1:]
        if period.period_end.year == latest_period.period_end.year - 1
        and (period.period_end.month - 1) // 3 == latest_quarter
    ]
    if not comparable_periods:
        return None
    prior_period = min(
        comparable_periods,
        key=lambda period: abs((latest_period.period_end - period.period_end).days - 365),
    )
    latest, prior = getattr(latest_period, field), getattr(prior_period, field)
    if not latest.available or not prior.available or prior.value is None or prior.value <= 0:
        return None
    return (latest.value / prior.value - 1) * 100


def trend_consistency(periods: Sequence[FinancialPeriod], field: str, count: int = 4) -> float | None:
    ordered = sorted(periods, key=lambda item: item.period_end)[-count:]
    values = [getattr(period, field).value for period in ordered]
    if len(values) < count or any(value is None for value in values):
        return None
    comparisons = [values[index] >= values[index - 1] for index in range(1, len(values))]
    return sum(comparisons) / len(comparisons) * 100
