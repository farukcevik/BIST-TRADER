from __future__ import annotations

from bistbot.app.config import ScoringSettings
from bistbot.app.models import (Action,EntryPlan,IntelligenceStatus,LLMAnalysis,LLMStatus,RankedEventCandidate,
                                SignalMode,StrategyDecision,TechnicalSignal)


class DeterministicStrategyEngine:
    """Scores available evidence only; it never sizes or executes orders."""
    def __init__(self,settings: ScoringSettings): self.settings=settings

    def evaluate(self,technical: TechnicalSignal,ranking: RankedEventCandidate,
                 analysis: LLMAnalysis, *, entry_plan: EntryPlan | None = None,
                 minimum_risk_reward_ratio: float | None = None) -> StrategyDecision:
        base={"technical":technical.technical_score,"trend":technical.trend_score,
              "liquidity":technical.liquidity_score,"momentum":technical.momentum_score,"volume":technical.volume_score}
        # Trend and liquidity are scanner evidence, not optional intelligence.
        # Preserve the configured aggregate technical weight while exposing its
        # three real contributions separately.
        weights={"technical":self.settings.technical*.6,"trend":self.settings.technical*.2,
                 "liquidity":self.settings.technical*.2,
                 "momentum":self.settings.momentum,"volume":self.settings.volume}
        mode=SignalMode.TECHNICAL_ONLY
        if ranking.intelligence_status is IntelligenceStatus.EVENTS_AVAILABLE:
            mode=SignalMode.TECHNICAL_PLUS_NEWS; base["news_kap"]=ranking.event_score; weights["news_kap"]=self.settings.news_kap
        if analysis.llm_status is LLMStatus.AVAILABLE:
            base["llm"]=(50+analysis.catalyst_score*analysis.confidence/200 if analysis.action_bias is Action.BUY
                         else analysis.confidence if analysis.action_bias is Action.SELL else 0)
            weights["llm"]=self.settings.llm
        weight_total=sum(weights.values()); normalized={key:value/weight_total for key,value in weights.items()}
        contributions={key:base[key]*normalized[key] for key in weights}
        raw_weighted=sum(base[key]*weights[key] for key in weights); score=sum(contributions.values())
        llm_blocks=(analysis.llm_status is LLMStatus.AVAILABLE and analysis.action_bias is Action.HOLD)
        if analysis.llm_status is LLMStatus.AVAILABLE and analysis.action_bias is Action.SELL:
            bearish={key:(100-value if key in {"technical","trend","liquidity","momentum","volume"} else value) for key,value in base.items()}
            score=sum(bearish[key]*weights[key] for key in weights)/weight_total
            action=Action.SELL if score>=100-self.settings.sell_threshold else Action.HOLD
        else:
            action=Action.BUY if not llm_blocks and score>=self.settings.buy_threshold else Action.HOLD
        rr_min=1.5 if minimum_risk_reward_ratio is None else minimum_risk_reward_ratio
        rr_blocked=(action is Action.BUY and entry_plan is not None and entry_plan.risk_reward_ratio < rr_min)
        if rr_blocked: action=Action.HOLD
        if rr_blocked: reason="INSUFFICIENT_RISK_REWARD"
        elif action is not Action.HOLD: reason=f"{mode.value} final_score {score:.2f} passed threshold"
        elif llm_blocks: reason="LLM action_bias is HOLD"
        else: reason=f"final_score {score:.2f} below buy_threshold {self.settings.buy_threshold:g}"
        configured={"technical":self.settings.technical,"momentum":self.settings.momentum,
            "volume":self.settings.volume,"news_kap":self.settings.news_kap,"llm":self.settings.llm}
        missing=[key for key in configured if key not in weights]
        reported_components=("technical","momentum","volume","trend","liquidity","news_kap","llm")
        reported_contributions={key:round(contributions.get(key,0.0),4) for key in reported_components}
        breakdown={"component_values":{**base,"trend":technical.trend_score,"liquidity":technical.liquidity_score},
            "available_weights":weights,"renormalized_weights":{k:round(v,6) for k,v in normalized.items()},
            "missing_components":missing,"contributions":reported_contributions,
            "raw_weighted_sum":round(raw_weighted,4),"weight_total":round(weight_total,6),"final_score":round(score,4)}
        if entry_plan is not None:
            breakdown["risk_reward"]={"actual":entry_plan.risk_reward_ratio,"minimum":rr_min,
                "eligible":not rr_blocked,"reason_code":"INSUFFICIENT_RISK_REWARD" if rr_blocked else None}
        return StrategyDecision(symbol=technical.symbol,action=action,final_score=round(score,4),reason=reason,
            signal_mode=mode,score_breakdown=breakdown)
