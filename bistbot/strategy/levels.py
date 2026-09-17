from __future__ import annotations

from enum import StrEnum
from datetime import datetime

from pydantic import BaseModel, Field

from bistbot.app.models import MarketBar
from bistbot.market.indicators import atr


class MarketStructure(StrEnum):
    HIGHER_HIGH_HIGHER_LOW="HIGHER_HIGH_HIGHER_LOW"
    LOWER_HIGH_LOWER_LOW="LOWER_HIGH_LOWER_LOW"
    RANGE="RANGE"
    MIXED="MIXED"


class BreakoutState(StrEnum):
    BREAKOUT="BREAKOUT"
    FAILED_BREAKOUT="FAILED_BREAKOUT"
    NONE="NONE"


class TechnicalLevels(BaseModel):
    symbol: str
    timestamp: datetime
    support_1: float | None = Field(default=None,gt=0)
    support_2: float | None = Field(default=None,gt=0)
    resistance_1: float | None = Field(default=None,gt=0)
    resistance_2: float | None = Field(default=None,gt=0)
    strong_support_zones: list[float] = Field(default_factory=list)
    strong_resistance_zones: list[float] = Field(default_factory=list)
    distance_to_support_pct: float | None = None
    distance_to_resistance_pct: float | None = None
    support_distance_atr: float | None = None
    resistance_distance_atr: float | None = None
    breakout_state: BreakoutState=BreakoutState.NONE
    market_structure: MarketStructure=MarketStructure.MIXED
    confidence: float=Field(ge=0,le=100)
    swing_highs: list[float]=Field(default_factory=list)
    swing_lows: list[float]=Field(default_factory=list)


def analyze_levels(bars: list[MarketBar], swing_window: int=3, zone_atr_fraction: float=.35,
        dedup_atr_fraction: float=.05, tick_size: float=.01) -> TechnicalLevels:
    """Uses only supplied bars; caller must truncate them at the decision timestamp."""
    if len(bars)<max(15,swing_window*2+1): raise ValueError("insufficient bars for levels")
    if any(bars[i].timestamp>=bars[i+1].timestamp for i in range(len(bars)-1)): raise ValueError("bars must be ordered")
    highs=[]; lows=[]
    for index in range(swing_window,len(bars)-swing_window):
        segment=bars[index-swing_window:index+swing_window+1]
        if bars[index].high==max(item.high for item in segment): highs.append((index,bars[index].high))
        if bars[index].low==min(item.low for item in segment): lows.append((index,bars[index].low))
    price=bars[-1].close; atr_value=atr([b.high for b in bars],[b.low for b in bars],[b.close for b in bars],14)
    epsilon=max(tick_size,atr_value*dedup_atr_fraction)
    def unique(points,*,reverse=False):
        result=[]
        for point in sorted(points,reverse=reverse):
            if not any(abs(point-existing)<=epsilon for existing in result): result.append(point)
        return result
    supports=unique((value for _,value in lows if value<price),reverse=True)
    resistances=unique((value for _,value in highs if value>price))
    s1=supports[0] if supports else None; s2=supports[1] if len(supports)>1 else None
    r1=resistances[0] if resistances else None; r2=resistances[1] if len(resistances)>1 else None
    def zones(points):
        result=[]
        for point in points:
            if sum(abs(other-point)<=atr_value*zone_atr_fraction for other in points)>=2 and not any(abs(existing-point)<=atr_value*zone_atr_fraction for existing in result): result.append(point)
        return result
    structure=MarketStructure.MIXED
    if len(highs)>=2 and len(lows)>=2:
        h_up=highs[-1][1]>highs[-2][1]; l_up=lows[-1][1]>lows[-2][1]
        structure=MarketStructure.HIGHER_HIGH_HIGHER_LOW if h_up and l_up else MarketStructure.LOWER_HIGH_LOWER_LOW if not h_up and not l_up else MarketStructure.MIXED
        if abs(highs[-1][1]-highs[-2][1])<=atr_value*.5 and abs(lows[-1][1]-lows[-2][1])<=atr_value*.5: structure=MarketStructure.RANGE
    prior_high=max(b.high for b in bars[:-1]); state=BreakoutState.BREAKOUT if price>prior_high else BreakoutState.FAILED_BREAKOUT if bars[-1].high>prior_high and price<=prior_high else BreakoutState.NONE
    confidence=min(100,(min(len(highs),4)+min(len(lows),4))/8*80+20)
    return TechnicalLevels(symbol=bars[-1].symbol,timestamp=bars[-1].timestamp,support_1=s1,support_2=s2,resistance_1=r1,resistance_2=r2,
        strong_support_zones=zones(supports),strong_resistance_zones=zones(resistances),
        distance_to_support_pct=(price-s1)/price*100 if s1 else None,distance_to_resistance_pct=(r1-price)/price*100 if r1 else None,
        support_distance_atr=(price-s1)/atr_value if s1 else None,resistance_distance_atr=(r1-price)/atr_value if r1 else None,
        breakout_state=state,market_structure=structure,confidence=confidence,
        swing_highs=unique(value for _,value in highs),swing_lows=unique((value for _,value in lows),reverse=True))
