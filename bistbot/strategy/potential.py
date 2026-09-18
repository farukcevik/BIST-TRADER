from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from enum import StrEnum
from math import isfinite
from pydantic import BaseModel, Field, model_validator

from bistbot.app.models import MarketRegime
from .levels import TechnicalLevels


class HoldingHorizon(StrEnum):
    INTRADAY="intraday"
    SWING="swing"


class PotentialAssessment(BaseModel):
    symbol: str
    decision_timestamp: datetime
    available: bool = True
    unavailable_reason: str | None = None
    potential_score: float | None=Field(default=None,ge=0,le=100)
    expected_target_price: float | None=Field(default=None,gt=0)
    expected_upside_pct: float | None = None
    target_confidence: float=Field(ge=0,le=100)
    target_method: str | None = None
    target_components: dict[str,float]=Field(default_factory=dict)
    downside_reference: float | None=Field(default=None,gt=0)
    downside_risk_pct: float | None = None
    target_1: float | None=Field(default=None,gt=0)
    target_2: float | None=Field(default=None,gt=0)
    target_3: float | None=Field(default=None,gt=0)
    rr_t1: float | None = None
    rr_t2: float | None = None
    rr_t3: float | None = None
    entry_rr: float | None = None
    risk_reward_ratio: float | None = None
    holding_horizon: HoldingHorizon=HoldingHorizon.SWING

    @model_validator(mode="after")
    def availability_matches_values(self) -> "PotentialAssessment":
        required=(self.potential_score,self.expected_target_price,self.expected_upside_pct,
            self.downside_reference,self.downside_risk_pct,self.target_1,
            self.target_2,self.rr_t1,self.rr_t2,self.entry_rr,self.risk_reward_ratio)
        derived=(*required,self.target_3,self.rr_t3)
        if self.available and any(value is None for value in required):
            raise ValueError("available potential requires complete price and risk fields")
        if not self.available and any(value is not None for value in derived):
            raise ValueError("unavailable potential cannot expose derived price or risk fields")
        if self.available:
            if not self.target_1 < self.target_2 or (self.target_3 is not None and not self.target_2 < self.target_3):
                raise ValueError("potential targets must be strictly ascending")
            if (self.target_3 is None) != (self.rr_t3 is None):
                raise ValueError("target_3 and rr_t3 must be provided together")
            if abs(self.entry_rr-self.risk_reward_ratio)>1e-4:
                raise ValueError("risk_reward_ratio must preserve the entry_rr compatibility alias")
        return self


def assess_potential(entry_price: float | None, stop_price: float | None, levels: TechnicalLevels, atr_value: float,
        trend_strength: float, momentum: float, relative_volume: float, technical_score: float,
        fundamental_score: float | None, catalyst_score: float, market_regime: MarketRegime=MarketRegime.NORMAL,
        sector_adjustment: float=0, catalyst_extension_limit_atr: float=.25, *,
        paper_slippage_pct: float=0, zone_atr_fraction: float=.35, tick_size: float=.01) -> PotentialAssessment:
    regime_factor={MarketRegime.RISK_ON:1,MarketRegime.NORMAL:.9,MarketRegime.CAUTION:.7,MarketRegime.RISK_OFF:.45,MarketRegime.CRISIS:.2}[market_regime]
    # Confidence describes the evidence supplied to this assessment, so it remains
    # meaningful even when price/structure validation makes the potential unavailable.
    confidence=(levels.confidence*.35+technical_score*.25+min(100,relative_volume*50)*.1+momentum*.1)
    if fundamental_score is not None: confidence+=fundamental_score*.2
    confidence*=regime_factor
    confidence=max(0,min(100,confidence+max(-10,min(10,sector_adjustment))))
    confidence=round(confidence,4)
    if entry_price is None or not isinstance(entry_price,(int,float)) or not isfinite(entry_price) or entry_price<=0:
        return PotentialAssessment(symbol=levels.symbol,decision_timestamp=levels.timestamp,available=False,
            unavailable_reason="ENTRY_PRICE_UNAVAILABLE",target_confidence=confidence)
    if (not isinstance(paper_slippage_pct,(int,float)) or not isfinite(paper_slippage_pct)
            or paper_slippage_pct<0 or not isinstance(zone_atr_fraction,(int,float))
            or not isfinite(zone_atr_fraction) or zone_atr_fraction<=0
            or not isinstance(tick_size,(int,float)) or not isfinite(tick_size) or tick_size<=0):
        return PotentialAssessment(symbol=levels.symbol,decision_timestamp=levels.timestamp,available=False,
            unavailable_reason="INVALID_POTENTIAL_CALIBRATION",target_confidence=confidence)
    # Mirror PaperBroker's BUY fill arithmetic and price-step rounding exactly.
    execution_price=float((Decimal(str(entry_price))*(Decimal("1")+Decimal(str(paper_slippage_pct))))
        .quantize(Decimal("0.0001"),rounding=ROUND_HALF_UP))
    if stop_price is None or not isfinite(stop_price) or not isfinite(atr_value) or atr_value<=0 or not 0<stop_price<entry_price:
        return PotentialAssessment(symbol=levels.symbol,decision_timestamp=levels.timestamp,available=False,
            unavailable_reason="INVALID_STOP_OR_ATR",target_confidence=confidence)
    supports=[value for value in [levels.support_1,levels.support_2,*levels.strong_support_zones,
        *levels.swing_lows] if value is not None and isfinite(value) and 0<value<execution_price]
    if not supports:
        return PotentialAssessment(symbol=levels.symbol,decision_timestamp=levels.timestamp,available=False,
            unavailable_reason="STRUCTURAL_SUPPORT_UNAVAILABLE",target_confidence=confidence)
    structural_support=max(supports)
    # The legacy supplied stop is validated for API compatibility but does not constrain
    # the dynamic result. A structure buffer avoids placing the stop exactly on support.
    required_distance=max(execution_price*.03,atr_value*1.5,
        execution_price-structural_support+atr_value*.25)
    if required_distance/execution_price>0.08:
        return PotentialAssessment(symbol=levels.symbol,decision_timestamp=levels.timestamp,available=False,
            unavailable_reason="REQUIRED_STOP_EXCEEDS_8_PERCENT",target_confidence=confidence)
    # Stop metadata is persisted at four decimals. Round toward the entry so the
    # exposed/RiskEngine stop cannot represent more than the validated 8% risk.
    dynamic_stop=float(Decimal(str(execution_price-required_distance))
        .quantize(Decimal("0.0001"),rounding=ROUND_CEILING))
    named_candidates=[("resistance_1",levels.resistance_1),("resistance_2",levels.resistance_2),
        *(("strong_resistance",value) for value in levels.strong_resistance_zones),
        *(("swing_high",value) for value in levels.swing_highs)]
    # Apply the level-analysis zone boundary so T1/T2 cannot be two prices from
    # the same configured resistance zone.
    zone_separation=max(tick_size,atr_value*zone_atr_fraction)
    validated=[]
    for name,value in sorted(named_candidates,key=lambda item:item[1] if item[1] is not None else float("inf")):
        # A target inside the entry zone has no meaningful reward after PAPER slippage.
        if value is None or not isfinite(value) or value-execution_price<=zone_separation: continue
        if not any(abs(value-existing[1])<=zone_separation for existing in validated):
            validated.append((name,value))
    if len(validated)<2:
        return PotentialAssessment(symbol=levels.symbol,decision_timestamp=levels.timestamp,available=False,
            unavailable_reason="INSUFFICIENT_VALIDATED_TARGETS",target_confidence=confidence)
    selected=validated[:3]; target_1=selected[0][1]; target_2=selected[1][1]
    target_3=selected[2][1] if len(selected)>2 else None
    # Catalyst input (which may contain LLM context) deliberately cannot alter price targets.
    extension=0.0
    downside=(execution_price-dynamic_stop)/execution_price*100
    rr_t1=(target_1-execution_price)/(execution_price-dynamic_stop)
    rr_t2=(target_2-execution_price)/(execution_price-dynamic_stop)
    rr_t3=((target_3-execution_price)/(execution_price-dynamic_stop) if target_3 is not None else None)
    # Eligibility is deliberately based only on required T1/T2. T3 and the trailing
    # remainder can improve realized returns, but can never manufacture entry eligibility.
    raw_entry_rr=(rr_t1+rr_t2)/2
    # The persisted/gated value must never round a just-below-threshold RR upward.
    entry_rr=float(Decimal(str(raw_entry_rr)).quantize(Decimal("0.0001"),rounding=ROUND_FLOOR))
    upside=(target_1-execution_price)/execution_price*100
    potential=max(0,min(100,upside*8+entry_rr*12+(confidence-50)*.2))
    return PotentialAssessment(symbol=levels.symbol,decision_timestamp=levels.timestamp,
        available=True,potential_score=round(potential,4),expected_target_price=round(target_1,4),
        expected_upside_pct=round(upside,4),target_confidence=confidence,
        target_method="validated_structure",
        target_components={f"{name}_{index}":round(value,4) for index,(name,value) in enumerate(selected,1)} |
            {"paper_execution_price":round(execution_price,4),
             "bounded_catalyst_extension":round(extension,4),"t1_exit_pct":25.0,"t2_exit_pct":25.0,
             "remaining_t3_or_trailing_pct":50.0},
        downside_reference=round(dynamic_stop,4),downside_risk_pct=round(downside,4),
        target_1=round(target_1,4),target_2=round(target_2,4),
        target_3=round(target_3,4) if target_3 is not None else None,
        rr_t1=round(rr_t1,4),rr_t2=round(rr_t2,4),rr_t3=round(rr_t3,4) if rr_t3 is not None else None,
        entry_rr=entry_rr,risk_reward_ratio=entry_rr)
