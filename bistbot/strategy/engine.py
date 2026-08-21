from __future__ import annotations

from bistbot.app.config import ScoringSettings
from bistbot.app.models import (Action,IntelligenceStatus,LLMAnalysis,LLMStatus,RankedEventCandidate,
                                SignalMode,StrategyDecision,TechnicalSignal)


class DeterministicStrategyEngine:
    """Scores available evidence only; it never sizes or executes orders."""
    def __init__(self,settings: ScoringSettings): self.settings=settings

    def evaluate(self,technical: TechnicalSignal,ranking: RankedEventCandidate,
                 analysis: LLMAnalysis) -> StrategyDecision:
        base={"technical":technical.technical_score,"momentum":technical.momentum_score,"volume":technical.volume_score}
        weights={"technical":self.settings.technical,"momentum":self.settings.momentum,"volume":self.settings.volume}
        mode=SignalMode.TECHNICAL_ONLY
        if ranking.intelligence_status is IntelligenceStatus.EVENTS_AVAILABLE:
            mode=SignalMode.TECHNICAL_PLUS_NEWS; base["news_kap"]=ranking.event_score; weights["news_kap"]=self.settings.news_kap
        if analysis.llm_status is LLMStatus.AVAILABLE:
            base["llm"]=(analysis.catalyst_score*analysis.confidence/100 if analysis.action_bias is Action.BUY
                         else analysis.confidence if analysis.action_bias is Action.SELL else 0)
            weights["llm"]=self.settings.llm
        weight_total=sum(weights.values()); score=sum(base[key]*weights[key] for key in weights)/weight_total
        llm_blocks=(analysis.llm_status is LLMStatus.AVAILABLE and analysis.action_bias is Action.HOLD)
        if analysis.llm_status is LLMStatus.AVAILABLE and analysis.action_bias is Action.SELL:
            bearish={key:(100-value if key in {"technical","momentum","volume"} else value) for key,value in base.items()}
            score=sum(bearish[key]*weights[key] for key in weights)/weight_total
            action=Action.SELL if score>=100-self.settings.sell_threshold else Action.HOLD
        else:
            action=Action.BUY if not llm_blocks and score>=self.settings.buy_threshold else Action.HOLD
        if action is not Action.HOLD: reason=f"{mode.value} final_score {score:.2f} passed threshold"
        elif llm_blocks: reason="LLM action_bias is HOLD"
        else: reason=f"final_score {score:.2f} below buy_threshold {self.settings.buy_threshold:g}"
        return StrategyDecision(symbol=technical.symbol,action=action,final_score=round(score,4),reason=reason,signal_mode=mode)
