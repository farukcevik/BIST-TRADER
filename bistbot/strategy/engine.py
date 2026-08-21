from __future__ import annotations

from bistbot.app.config import ScoringSettings
from bistbot.app.models import Action,LLMAnalysis,RankedEventCandidate,StrategyDecision,TechnicalSignal
from .scoring import weighted_score


class DeterministicStrategyEngine:
    """Combines configured scores; it never sizes or executes orders."""
    def __init__(self,settings: ScoringSettings): self.settings=settings

    def evaluate(self,technical: TechnicalSignal,ranking: RankedEventCandidate,
                 analysis: LLMAnalysis) -> StrategyDecision:
        weights={key:value for key,value in self.settings.model_dump().items()
                 if key not in {"buy_threshold","sell_threshold"}}
        if analysis.action_bias is Action.SELL:
            components={"technical":100-technical.technical_score,"momentum":100-technical.momentum_score,
                "volume":100-technical.volume_score,"news_kap":ranking.event_score,"llm":analysis.confidence}
            score=weighted_score(components,weights)
            action=Action.SELL if score>=100-self.settings.sell_threshold else Action.HOLD
        else:
            llm_score=analysis.catalyst_score*analysis.confidence/100 if analysis.action_bias is Action.BUY else 0
            components={"technical":technical.technical_score,"momentum":technical.momentum_score,
                "volume":technical.volume_score,"news_kap":ranking.event_score,"llm":llm_score}
            score=weighted_score(components,weights)
            action=Action.BUY if analysis.action_bias is Action.BUY and score>=self.settings.buy_threshold else Action.HOLD
        return StrategyDecision(symbol=technical.symbol,action=action,final_score=round(score,4),
            reason=f"configured final score={score:.2f}; analyst bias={analysis.action_bias.value}")
