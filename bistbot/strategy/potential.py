from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from statistics import median
from typing import Sequence

from bistbot.app.config import PotentialSettings
from bistbot.app.models import (
    EntryPlan, HoldingHorizon, MarketBar, MarketRegime, TargetComponents,
    TargetConfidence, TechnicalSignal,
)


@dataclass(frozen=True)
class PotentialInput:
    technical: TechnicalSignal
    bars: Sequence[MarketBar]
    decision_timestamp: datetime
    market_regime: MarketRegime = MarketRegime.NORMAL
    sector_adjustment: float = 0.0
    catalyst_score: float = 0.0
    catalyst_confidence: float = 0.0


class PotentialEngine:
    """Deterministic target estimator using only bars known at decision time."""

    def __init__(self, settings: PotentialSettings | None = None):
        self.settings = settings or PotentialSettings()

    def evaluate(self, evidence: PotentialInput) -> EntryPlan:
        bars = self._validated_bars(evidence)
        signal = evidence.technical
        entry = float(signal.metrics.get("latest_price", bars[-1].close))
        atr = float(signal.metrics.get("atr_14", 0))
        if not math.isfinite(entry) or entry <= 0 or not math.isfinite(atr) or atr <= 0:
            raise ValueError("entry price and ATR must be positive and finite")

        highs = [bar.high for bar in bars]
        lows = [bar.low for bar in bars]
        resistance, swing_high = self._upside_levels(highs, entry)
        support, swing_low = self._downside_levels(lows, entry)
        potential = self._potential_score(evidence, entry, atr, resistance)

        strength = potential / 100
        atr_multiplier = self.settings.atr_target_min_multiplier + strength * (
            self.settings.atr_target_max_multiplier - self.settings.atr_target_min_multiplier
        )
        atr_target = entry + atr * atr_multiplier
        ema9 = float(signal.metrics.get("ema_9", entry))
        ema21 = float(signal.metrics.get("ema_21", entry))
        ema_spread = max(0.0, (ema9 - ema21) / entry)
        trend_target = entry + atr * (1.0 + 1.5 * strength) + entry * min(ema_spread, 0.06)

        structural = [value for value in (resistance, swing_high) if value is not None]
        candidates = structural + [atr_target, trend_target]
        # Median resists a single optimistic estimate; nearby structure remains influential.
        base_target = median(candidates)
        if resistance is not None and resistance <= entry + atr:
            base_target = min(base_target, resistance)
        catalyst_pct = self._catalyst_adjustment(evidence)
        target2 = max(entry + atr * 0.5, base_target * (1 + catalyst_pct))

        first_candidates = [value for value in structural if entry < value < target2]
        target1 = min(first_candidates) if first_candidates else entry + (target2 - entry) * 0.55
        extension = max(atr * (0.45 + strength * 0.55), (target2 - entry) * (0.2 + strength * 0.3))
        target3 = target2 + extension

        stop_distance = min(self.settings.stop_max_pct, max(self.settings.stop_min_pct, atr / entry * 1.5))
        atr_stop = entry * (1 - stop_distance)
        structural_stops = [value for value in (support, swing_low) if value is not None]
        structure_stop = max(structural_stops) if structural_stops else atr_stop
        raw_stop = min(atr_stop, structure_stop * 0.997)
        stop = min(entry * (1 - self.settings.stop_min_pct), max(entry * (1 - self.settings.stop_max_pct), raw_stop))

        downside = (entry - stop) / entry
        upside = (target2 - entry) / entry
        rr = upside / downside
        horizon = self._horizon(upside, strength)
        confidence = (TargetConfidence.HIGH if potential >= 75 and len(structural) >= 1 else
                      TargetConfidence.MEDIUM if potential >= 50 else TargetConfidence.LOW)
        components = TargetComponents(
            resistance_target=resistance, swing_high_target=swing_high,
            atr_target=round(atr_target, 6), trend_extension_target=round(trend_target, 6),
            catalyst_adjustment_pct=round(catalyst_pct, 6), support_price=support,
            swing_low_price=swing_low, atr_stop_price=round(atr_stop, 6),
            selected_base_target=round(base_target, 6), decision_timestamp=evidence.decision_timestamp,
            bars_used=len(bars),
            atr_value=round(atr, 6), trend_score=signal.trend_score,
            market_regime=evidence.market_regime,
        )
        return EntryPlan(symbol=signal.symbol, entry_price=round(entry, 6), initial_stop_price=round(stop, 6),
            target_1=round(target1, 6), target_2=round(target2, 6), target_3=round(target3, 6),
            expected_upside_pct=round(upside, 6), downside_risk_pct=round(downside, 6),
            risk_reward_ratio=round(rr, 6), potential_score=round(potential, 4),
            holding_horizon=horizon, target_confidence=confidence,
            target_method="CONSERVATIVE_MULTI_CANDIDATE_MEDIAN",
            target_components=components)

    def revaluation_trigger(self, existing: EntryPlan, evidence: PotentialInput) -> str | None:
        """Return one auditable material-change reason, never a cycle-based trigger."""
        if not self.settings.target_revaluation_enabled:
            return None
        bars=self._validated_bars(evidence); old=existing.target_components
        atr=float(evidence.technical.metrics.get("atr_14",0))
        if old.market_regime is not None and evidence.market_regime!=old.market_regime:
            return "MARKET_REGIME_CHANGE"
        if old.trend_score is not None and old.trend_score-evidence.technical.trend_score>=self.settings.revaluation_trend_drop_points:
            return "TREND_DETERIORATION"
        if old.atr_value and abs(atr-old.atr_value)/old.atr_value>=self.settings.revaluation_volatility_change_pct:
            return "VOLATILITY_REGIME_CHANGE"
        known=max((value for value in (old.resistance_target,old.swing_high_target) if value),default=existing.entry_price)
        if max(bar.high for bar in bars[-20:])>=known+atr*self.settings.revaluation_swing_atr_threshold:
            return "SIGNIFICANT_NEW_SWING_HIGH"
        if bars[-1].close>=existing.target_1:
            return "TARGET_REACHED"
        return None

    def re_evaluate(self, existing: EntryPlan, evidence: PotentialInput, trigger: str) -> EntryPlan:
        """Bound a material revision; the original stop can only tighten."""
        allowed={"MARKET_REGIME_CHANGE","TREND_DETERIORATION","VOLATILITY_REGIME_CHANGE",
                 "SIGNIFICANT_NEW_SWING_HIGH","TARGET_REACHED","MATERIAL_CATALYST"}
        if trigger not in allowed:
            raise ValueError("unsupported target revaluation trigger")
        candidate=self.evaluate(evidence); entry=existing.entry_price
        def bounded(old:float,new:float)->float:
            lower=max(entry*(1+1e-6),old*(1-self.settings.target_revision_max_down_pct))
            upper=old*(1+self.settings.target_revision_max_up_pct)
            return min(upper,max(lower,new))
        t1=bounded(existing.target_1,candidate.target_1)
        t2=max(t1,bounded(existing.target_2,candidate.target_2))
        t3=max(t2,bounded(existing.target_3,candidate.target_3))
        stop=max(existing.initial_stop_price,candidate.initial_stop_price)
        downside=(entry-stop)/entry; upside=(t2-entry)/entry
        if downside<=0: raise ValueError("revaluation cannot invalidate stop distance")
        return candidate.model_copy(update={"entry_price":entry,"initial_stop_price":stop,
            "target_1":t1,"target_2":t2,"target_3":t3,
            "expected_upside_pct":upside,"downside_risk_pct":downside,
            "risk_reward_ratio":upside/downside,
            "target_method":f"{candidate.target_method}:REVALUATED:{trigger}"})

    def _validated_bars(self, evidence: PotentialInput) -> list[MarketBar]:
        if not evidence.bars:
            raise ValueError("bars are required")
        cutoff = self._utc(evidence.decision_timestamp)
        bars = list(evidence.bars)
        if self._utc(evidence.technical.timestamp) > cutoff:
            raise ValueError("future technical signal is not permitted")
        if any(bar.symbol != evidence.technical.symbol for bar in bars):
            raise ValueError("bar symbol mismatch")
        if any(self._utc(bar.timestamp) > cutoff for bar in bars):
            raise ValueError("future bars are not permitted")
        if any(bars[index].timestamp >= bars[index + 1].timestamp for index in range(len(bars) - 1)):
            raise ValueError("bars must be strictly ordered")
        return bars

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

    @staticmethod
    def _local_extrema(values: Sequence[float], *, high: bool) -> list[float]:
        comparator = max if high else min
        return [values[i] for i in range(1, len(values) - 1)
                if values[i] == comparator(values[i - 1:i + 2])]

    def _upside_levels(self, highs: Sequence[float], entry: float) -> tuple[float | None, float | None]:
        recent = list(highs[-60:-1])
        extrema = sorted({value for value in self._local_extrema(recent, high=True) if value > entry})
        resistance = extrema[0] if extrema else None
        swing = max((value for value in recent[-20:] if value > entry), default=None)
        return resistance, swing

    def _downside_levels(self, lows: Sequence[float], entry: float) -> tuple[float | None, float | None]:
        recent = list(lows[-60:-1])
        extrema = sorted({value for value in self._local_extrema(recent, high=False) if value < entry}, reverse=True)
        support = extrema[0] if extrema else None
        swing = min((value for value in recent[-20:] if value < entry), default=None)
        return support, swing

    def _potential_score(self, evidence: PotentialInput, entry: float, atr: float,
                         resistance: float | None) -> float:
        signal = evidence.technical
        relative_volume = float(signal.metrics.get("relative_volume", 1.0))
        rv_score = min(100.0, max(0.0, 40 + relative_volume * 30))
        room = ((resistance - entry) / atr * 18 + 35) if resistance is not None else 65
        room_score = min(100.0, max(0.0, room))
        regime_score = {MarketRegime.RISK_ON: 85, MarketRegime.NORMAL: 65,
            MarketRegime.CAUTION: 45, MarketRegime.RISK_OFF: 25, MarketRegime.CRISIS: 0}[evidence.market_regime]
        catalyst = min(100.0, max(0.0, evidence.catalyst_score)) * min(1.0, max(0.0, evidence.catalyst_confidence / 100))
        score = (signal.trend_score * .20 + signal.momentum_score * .18 + signal.volume_score * .12 +
                 rv_score * .08 + signal.technical_score * .15 + signal.overall_scanner_score * .08 +
                 room_score * .08 + regime_score * .06 + catalyst * .03 +
                 min(100.0, max(0.0, 50 + evidence.sector_adjustment * 10)) * .02)
        return min(100.0, max(0.0, score))

    def _catalyst_adjustment(self, evidence: PotentialInput) -> float:
        strength = min(100.0, max(0.0, evidence.catalyst_score)) / 100
        confidence = min(100.0, max(0.0, evidence.catalyst_confidence)) / 100
        return min(self.settings.catalyst_adjustment_max_pct,
                   self.settings.catalyst_adjustment_max_pct * strength * confidence)

    @staticmethod
    def _horizon(upside: float, strength: float) -> HoldingHorizon:
        if upside <= .035: return HoldingHorizon.INTRADAY
        if upside <= .08: return HoldingHorizon.SHORT_SWING
        if upside <= .16 or strength < .8: return HoldingHorizon.SWING
        return HoldingHorizon.POSITION
