from __future__ import annotations

import math
from collections.abc import Sequence


def simple_moving_average(values: Sequence[float], period: int) -> float | None:
    if period <= 0: raise ValueError("period must be positive")
    if len(values) < period: return None
    return sum(values[-period:]) / period


def ema(values: Sequence[float], period: int) -> float:
    if period <= 0 or len(values) < period: raise ValueError("insufficient values for EMA")
    value = sum(values[:period]) / period
    multiplier = 2 / (period + 1)
    for price in values[period:]: value = (price - value) * multiplier + value
    return value


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float, float]:
    if len(values) < slow + signal - 1: raise ValueError("insufficient values for MACD")
    line_series=[]
    for end in range(slow,len(values)+1):
        subset=values[:end]; line_series.append(ema(subset,fast)-ema(subset,slow))
    signal_value=ema(line_series,signal)
    return line_series[-1],signal_value,line_series[-1]-signal_value


def rsi(values: Sequence[float], period: int = 14) -> float:
    if len(values) < period + 1: raise ValueError("insufficient values for RSI")
    changes = [values[i] - values[i-1] for i in range(1, len(values))]
    gains = [max(change, 0) for change in changes[-period:]]
    losses = [max(-change, 0) for change in changes[-period:]]
    average_gain, average_loss = sum(gains)/period, sum(losses)/period
    if average_loss == 0: return 100.0 if average_gain else 50.0
    return 100 - 100 / (1 + average_gain / average_loss)


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> float:
    if not (len(highs) == len(lows) == len(closes)) or len(closes) < period + 1:
        raise ValueError("insufficient values for ATR")
    ranges = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
              for i in range(1, len(closes))]
    return sum(ranges[-period:]) / period


def return_pct(current: float, previous: float) -> float:
    if previous <= 0: raise ValueError("previous price must be positive")
    return (current / previous - 1) * 100


def volatility_pct(values: Sequence[float], period: int = 14) -> float:
    if len(values) < period + 1: raise ValueError("insufficient values for volatility")
    returns = [values[i]/values[i-1]-1 for i in range(len(values)-period, len(values))]
    mean = sum(returns) / len(returns)
    variance = sum((item-mean)**2 for item in returns) / len(returns)
    return math.sqrt(variance) * 100
