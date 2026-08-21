from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bistbot.app.config import ScannerSettings
from bistbot.app.models import MarketBar, TechnicalSignal
from bistbot.storage.repositories import TechnicalSignalRepository
from .indicators import atr, ema, return_pct, rsi, volatility_pct


def _clamp(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 4)


class InvalidMarketData(ValueError):
    pass


class DeterministicMarketScanner:
    """OHLCV-only first-stage scanner. This module never calls an LLM."""
    def __init__(self, settings: ScannerSettings, repository: TechnicalSignalRepository | None = None):
        self.settings, self.repository = settings, repository; self.last_signals=[]

    def scan(self, histories: Mapping[str, Sequence[MarketBar]], limit: int = 40,
             now: datetime | None = None) -> list[TechnicalSignal]:
        if limit <= 0: raise ValueError("limit must be positive")
        now = now or datetime.now(timezone.utc)
        signals: list[TechnicalSignal] = []
        for symbol in sorted(histories):
            try:
                signal = self.score(symbol, histories[symbol], now)
            except (InvalidMarketData, ValueError, ArithmeticError):
                continue
            if signal is not None: signals.append(signal)
        signals.sort(key=lambda item: (-item.overall_scanner_score, item.symbol))
        self.last_signals=list(signals)
        selected = signals[:limit]
        if self.repository:
            for rank, signal in enumerate(selected, 1): self.repository.add(signal, rank)
        return selected

    def score(self, symbol: str, bars: Sequence[MarketBar], now: datetime) -> TechnicalSignal | None:
        self._validate(symbol, bars, now)
        closes = [bar.close for bar in bars]; highs = [bar.high for bar in bars]
        lows = [bar.low for bar in bars]; volumes = [bar.volume for bar in bars]
        close = closes[-1]
        one_bar = return_pct(close, closes[-2])
        momentum_3 = return_pct(close, closes[-4])
        momentum_12 = return_pct(close, closes[-13])
        ema_9, ema_21 = ema(closes, 9), ema(closes, 21)
        rsi_14 = rsi(closes, 14); atr_14 = atr(highs, lows, closes, 14)
        lookback = self.settings.recent_high_lookback
        prior_high = max(highs[-lookback-1:-1]) if len(highs) > lookback else max(highs[:-1])
        recent_high_distance = return_pct(close, prior_high)
        breakout = close > prior_high
        completed=[index for index,volume in enumerate(volumes) if volume>0]
        if len(completed)<2: raise InvalidMarketData("insufficient completed volume bars")
        volume_index=completed[-1]
        istanbul=ZoneInfo("Europe/Istanbul")
        latest_local=bars[volume_index].timestamp.astimezone(istanbul)
        distinct_dates={bars[index].timestamp.astimezone(istanbul).date() for index in completed}
        if len(distinct_dates)>1:
            history_indices=[index for index in completed[:-1]
                if bars[index].timestamp.astimezone(istanbul).hour==latest_local.hour][-self.settings.relative_volume_lookback:]
            volume_method="SAME_HOURLY_INTERVAL_ISTANBUL"
        else:
            history_indices=completed[:-1][-self.settings.relative_volume_lookback:]
            volume_method="ROLLING_EQUAL_DURATION"
        if not history_indices: raise InvalidMarketData("no historical same-interval volume")
        rv_period=len(history_indices); latest_volume=volumes[volume_index]
        average_volume=sum(volumes[index] for index in history_indices)/rv_period
        relative_volume=latest_volume/average_volume if average_volume>0 else 0
        average_turnover=sum(closes[index]*volumes[index] for index in history_indices)/rv_period
        volatility = volatility_pct(closes, 14); atr_pct = atr_14 / close * 100
        continuity=sum(volumes[index]>0 for index in range(max(0,volume_index-rv_period+1),volume_index+1))/rv_period
        volume_liquidity=_clamp(50+20*math.log10(max(average_volume/self.settings.minimum_average_volume,.01)))
        turnover_liquidity=_clamp(50+20*math.log10(max(average_turnover/self.settings.minimum_average_turnover_try,.01)))
        liquidity_score=_clamp(volume_liquidity*.40+turnover_liquidity*.45+continuity*100*.15)
        if average_volume < self.settings.minimum_average_volume or average_turnover < self.settings.minimum_average_turnover_try:
            return None
        momentum_score = _clamp(50 + one_bar*3 + momentum_3*2 + momentum_12)
        trend_spread = return_pct(ema_9, ema_21)
        trend_score = _clamp(50 + trend_spread*7)
        rsi_adjustment = 10 if 50 <= rsi_14 <= 70 else (-25 if rsi_14 >= 80 else -8 if rsi_14 >= 70 else 0)
        technical_score = _clamp(50 + trend_spread*7 + (12 if breakout else recent_high_distance*.5) + rsi_adjustment)
        if rsi_14 >= 80:
            technical_score = min(technical_score, 70.0)
        volume_score = _clamp(20 + relative_volume*30)
        distance = abs(volatility-self.settings.target_volatility_pct)
        volatility_score = _clamp(100 - distance*20)
        if volatility > self.settings.maximum_preferred_volatility_pct:
            volatility_score = _clamp(volatility_score - 25)
        component_scores = {"technical":technical_score,"momentum":momentum_score,"volume":volume_score,
                            "liquidity":liquidity_score,"volatility":volatility_score}
        weights = self.settings.weights.model_dump()
        overall = _clamp(sum(component_scores[key]*weights[key] for key in weights))
        reasons = []
        if ema_9 > ema_21: reasons.append("EMA9 above EMA21")
        if momentum_12 > 0: reasons.append("positive 12-bar momentum")
        if relative_volume >= 1.5: reasons.append("relative volume spike")
        if breakout: reasons.append("recent-high breakout")
        if rsi_14 >= 80: reasons.append("overbought RSI penalty")
        return TechnicalSignal(symbol=symbol, timestamp=bars[-1].timestamp,
            technical_score=technical_score, momentum_score=momentum_score, volume_score=volume_score,trend_score=trend_score,
            liquidity_score=liquidity_score, volatility_score=volatility_score,
            overall_scanner_score=overall, metrics={"return_1":one_bar,"momentum_3":momentum_3,
            "momentum_12":momentum_12,"relative_volume":relative_volume,"ema_9":ema_9,"ema_21":ema_21,
            "rsi_14":rsi_14,"atr_14":atr_14,"atr_pct":atr_pct,"recent_high_distance":recent_high_distance,
            "breakout":breakout,"average_volume":average_volume,"average_turnover_try":average_turnover,
            "latest_price":close,"latest_volume":latest_volume,"trading_continuity":continuity,
            "volume_method":volume_method,"current_interval_volume":latest_volume,
            "historical_comparable_volume":average_volume,
            "volatility_pct":volatility}, reasons=reasons)

    def _validate(self, symbol: str, bars: Sequence[MarketBar], now: datetime) -> None:
        if len(bars) < self.settings.minimum_bars: raise InvalidMarketData("missing bars")
        if any(bar.symbol != symbol for bar in bars): raise InvalidMarketData("symbol mismatch")
        if any(bars[i].timestamp >= bars[i+1].timestamp for i in range(len(bars)-1)):
            raise InvalidMarketData("bars must be strictly ordered")
        if any(bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close) or bar.high < bar.low
               for bar in bars): raise InvalidMarketData("invalid OHLC")
        numeric = (value for bar in bars for value in (bar.open,bar.high,bar.low,bar.close,bar.volume))
        if not all(math.isfinite(value) for value in numeric): raise InvalidMarketData("non-finite data")
        latest = bars[-1].timestamp
        if latest.tzinfo is None: latest = latest.replace(tzinfo=timezone.utc)
        reference = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        age_minutes = (reference.astimezone(timezone.utc)-latest.astimezone(timezone.utc)).total_seconds()/60
        if age_minutes < 0 or age_minutes > self.settings.max_data_age_minutes:
            raise InvalidMarketData("stale market data")
