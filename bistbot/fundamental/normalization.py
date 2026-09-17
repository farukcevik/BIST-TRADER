from __future__ import annotations

from collections.abc import Sequence

from .models import FinancialPeriod


def safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def yoy_growth(periods: Sequence[FinancialPeriod], field: str) -> float | None:
    """Latest quarter against the corresponding quarter one year earlier."""
    if len(periods) < 5:
        return None
    ordered = sorted(periods, key=lambda item: item.period_end, reverse=True)
    latest, prior = getattr(ordered[0], field), getattr(ordered[4], field)
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
