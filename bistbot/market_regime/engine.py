from __future__ import annotations

from datetime import datetime, timedelta, timezone
from bistbot.app.config import MarketRegimeSettings
from bistbot.app.models import (Action, MacroEvent, MacroRiskCategory, MarketOverlay, MarketRegime,
    MarketRegimeState, StrategyDecision)
from .materiality import classify_macro_event
from .sectors import sector_for_symbol

class MarketRegimeEngine:
    """Deterministic, cached macro risk layer. It never submits or sizes an order."""
    def __init__(self,settings: MarketRegimeSettings,provider=None,*,now=None):
        self.settings=settings; self.provider=provider; self.now=now or (lambda:datetime.now(timezone.utc))
        self._last_refresh: datetime|None=None; self._events: dict[str,MacroEvent]={}; self._state=MarketRegimeState()
        self.last_diagnostics={"events_fetched":0,"events_material":0,"events_rejected":0,
            "events_sent_to_llm":0,"latest_material_event":None,"provider_status":"UNAVAILABLE"}

    def refresh(self,events: list[MacroEvent]|None=None,*,force: bool=False) -> MarketRegimeState:
        now=self.now()
        if not force and events is None and self._last_refresh and now-self._last_refresh<timedelta(minutes=self.settings.refresh_minutes):
            return self._state
        incoming=events if events is not None else (self.provider.fetch() if self.provider else [])
        for event in incoming:
            classified=classify_macro_event(event,now=now)
            previous=self._events.get(classified.canonical_event_id)
            if previous is None or previous.model_dump()!=classified.model_dump(): self._events[classified.canonical_event_id]=classified
        self._last_refresh=now; self._state=self.evaluate(list(self._events.values()),now=now); return self._state

    def evaluate(self,events: list[MacroEvent],*,now: datetime|None=None) -> MarketRegimeState:
        now=now or self.now(); material=[classify_macro_event(e,now=now) for e in events]
        # Background events remain persisted/diagnosable but cannot elevate the
        # current regime unless their freshness-adjusted impact is meaningful.
        material=[e for e in material if e.materiality_score>=40 and e.effective_materiality>=10 and e.freshness_weight>0]
        categories={category.value:0 for category in MacroRiskCategory}
        for event in material:
            if event.direction in {"NEGATIVE","MIXED"}:
                categories[event.category.value]=max(categories[event.category.value],round(event.regime_contribution))
        scores=sorted(categories.values(),reverse=True); risk=round(min(100,(scores[0] if scores else 0)+(scores[1] if len(scores)>1 else 0)*.2))
        positive_signal=max((e.effective_materiality for e in material if e.direction=="POSITIVE"),default=0)
        regime=(MarketRegime.CRISIS if risk>=90 else MarketRegime.RISK_OFF if risk>=70 else
                MarketRegime.CAUTION if risk>=40 else MarketRegime.RISK_ON if positive_signal>=70 else MarketRegime.NORMAL)
        confidence=round(min(100,45+max((e.source_reliability for e in material),default=35)*.45))
        negative=sorted({s for e in material for s in e.negative_sectors}); positive=sorted({s for e in material for s in e.positive_sectors})
        affected=sorted({s for e in material for s in e.affected_sectors}|set(negative)|set(positive))
        state=MarketRegimeState(regime=regime,market_risk_score=risk,confidence=confidence,
            risk_categories=categories,event_summary="; ".join(e.title for e in material[:3]) or "No material macro event",
            expected_market_direction="NEGATIVE" if risk>=40 else "NEUTRAL",expected_duration="1-7 days" if material else "unknown",
            affected_sectors=affected,positive_sectors=positive,negative_sectors=negative,
            uncertainty="Deterministic classification; confirm evolving events with primary sources.",
            source_ids=[e.source_id for e in material],material_events=material,last_updated=now)
        provider_diagnostics=getattr(self.provider,"last_diagnostics",{}) if self.provider else {}
        self.last_diagnostics={"events_fetched":len(events),"events_material":len(material),
            "events_rejected":max(0,len(events)-len(material)),"events_sent_to_llm":0,
            "latest_material_event":material[0].title if material else None,
            "provider_status":provider_diagnostics.get("provider_status",getattr(self.provider,"status","UNAVAILABLE")),
            "provider":type(self.provider).__name__ if self.provider else None,"sources":provider_diagnostics.get("sources",{})}
        return state

    def overlay(self,symbol: str,state: MarketRegimeState|None=None) -> MarketOverlay:
        state=state or self._state; behavior=self.settings.behaviors[state.regime.value]; sector=sector_for_symbol(symbol)
        sector_adjustment=(self.settings.sector_adjustment if sector in state.positive_sectors else
                           -self.settings.sector_adjustment if sector in state.negative_sectors else 0)
        return MarketOverlay(market_regime=state.regime,market_adjustment=behavior.score_adjustment,
            sector_adjustment=sector_adjustment,buy_threshold_adjustment=behavior.buy_threshold_adjustment,
            position_multiplier=behavior.position_multiplier,block_new_entries=behavior.block_new_entries)

def apply_market_overlay(decision: StrategyDecision,overlay: MarketOverlay,buy_threshold: float) -> StrategyDecision:
    base=decision.final_score; adjusted=max(0,min(100,base+overlay.market_adjustment+overlay.sector_adjustment))
    threshold=buy_threshold+overlay.buy_threshold_adjustment; action=decision.action
    if action is Action.BUY and (overlay.block_new_entries or adjusted<threshold): action=Action.HOLD
    breakdown={**decision.score_breakdown,"base_stock_score":base,"market_regime":overlay.market_regime.value,
        "market_adjustment":overlay.market_adjustment,"sector_adjustment":overlay.sector_adjustment,
        "adjusted_final_score":round(adjusted,4),"adjusted_buy_threshold":threshold,
        "position_multiplier":overlay.position_multiplier,"block_new_entries":overlay.block_new_entries}
    reason=(f"{overlay.market_regime.value} overlay: base {base:.2f}, market {overlay.market_adjustment:+g}, "
            f"sector {overlay.sector_adjustment:+g}, adjusted {adjusted:.2f}, threshold {threshold:g}")
    return decision.model_copy(update={"action":action,"final_score":round(adjusted,4),"reason":reason,"score_breakdown":breakdown})
