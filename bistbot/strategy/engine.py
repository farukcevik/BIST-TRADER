from __future__ import annotations

from bistbot.app.config import FundamentalUnavailablePolicy, ScoringSettings
from bistbot.app.models import (Action,IntelligenceStatus,LLMAnalysis,LLMStatus,RankedEventCandidate,
                                SignalMode,StrategyDecision,TechnicalSignal)
from bistbot.fundamental.models import FundamentalAssessment
from bistbot.intelligence.catalyst import CatalystAssessment
from bistbot.intelligence.catalyst import CatalystAvailability
from bistbot.strategy.potential import PotentialAssessment

from pydantic import BaseModel, Field
from typing import Literal


class InvestmentDecision(BaseModel):
    symbol: str
    action: Action
    reason_code: str
    reason: str
    final_score: float=Field(ge=0,le=100)
    fundamental_score: float|None=None
    technical_score: float
    catalyst_score: float|None=None
    potential_score: float | None
    expected_target_price: float | None
    expected_upside_pct: float | None
    risk_reward_ratio: float | None
    target_1: float | None=None
    target_2: float | None=None
    target_3: float | None=None
    rr_t1: float | None=None
    rr_t2: float | None=None
    rr_t3: float | None=None
    entry_rr: float | None=None
    buy_tier: Literal["NORMAL","FLEX"] | None=None
    position_size_multiplier: float=Field(default=0,ge=0,le=1)
    labels: list[str]=Field(default_factory=list)


class DeterministicStrategyEngine:
    """Scores available evidence only; it never sizes or executes orders."""
    def __init__(self,settings: ScoringSettings): self.settings=settings

    def evaluate(self,technical: TechnicalSignal,ranking: RankedEventCandidate,
                 analysis: LLMAnalysis) -> StrategyDecision:
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
        if action is not Action.HOLD: reason=f"{mode.value} final_score {score:.2f} passed threshold"
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
        return StrategyDecision(symbol=technical.symbol,action=action,final_score=round(score,4),reason=reason,
            signal_mode=mode,score_breakdown=breakdown)

    def evaluate_investment(self, technical: TechnicalSignal, fundamental: FundamentalAssessment,
            catalyst: CatalystAssessment, potential: PotentialAssessment, *, minimum_risk_reward_ratio: float=1.5,
            minimum_fundamental_score: float=50, minimum_technical_score: float|None=None,
            unavailable_policy: FundamentalUnavailablePolicy=FundamentalUnavailablePolicy.BLOCK,
            fallback_minimum_risk_reward_ratio: float=2.0, fallback_minimum_technical_score: float=75,
            fallback_minimum_potential_confidence: float=65, speculative_technical_score: float=80,
            speculative_catalyst_score: float=65, minimum_fundamental_confidence: float=40,
            minimum_catalyst_score: float=50, require_catalyst_confirmation: bool=True,
            paper_mode: bool=False) -> InvestmentDecision:
        """Staged eligibility gate. RiskEngine remains the mandatory next step after BUY."""
        if minimum_risk_reward_ratio<=0: raise ValueError("minimum risk/reward must be positive")
        technical_min=self.settings.buy_threshold if minimum_technical_score is None else minimum_technical_score
        labels=[]; reason_code="BUY_CANDIDATE"; action=Action.BUY
        fallback=(fundamental.provider_status.value=="UNAVAILABLE" or
            fundamental.fundamental_score is None or fundamental.effective_score is None)
        if fundamental.blocks_entry:
            action,reason_code=Action.HOLD,"FUNDAMENTAL_BLOCKING_RED_FLAG"
        elif fallback:
            if unavailable_policy is FundamentalUnavailablePolicy.BLOCK:
                action,reason_code=Action.HOLD,"FUNDAMENTAL_DATA_UNAVAILABLE"
            else:
                labels.append("FUNDAMENTAL_DATA_UNAVAILABLE_FALLBACK")
                if technical.technical_score<fallback_minimum_technical_score:
                    action,reason_code=Action.HOLD,"FUNDAMENTAL_FALLBACK_TECHNICAL_SCORE_TOO_LOW"
        else:
            if fundamental.provider_status.value=="PARTIAL":
                labels.extend(["FUNDAMENTAL_DATA_PARTIAL",
                    *[f"MISSING_FUNDAMENTAL_METRIC:{name}" for name in fundamental.missing_metrics]])
            if fundamental.confidence<minimum_fundamental_confidence:
                labels.append("FUNDAMENTAL_DATA_INCOMPLETE")
                action,reason_code=Action.HOLD,"FUNDAMENTAL_CONFIDENCE_LOW"
            elif fundamental.effective_score<minimum_fundamental_score:
                labels.append("SPECULATIVE_SETUP")
                action,reason_code=Action.HOLD,"FUNDAMENTAL_QUALITY_LOW"
        if action is Action.BUY and not fallback and technical.technical_score<technical_min:
            action,reason_code=Action.HOLD,"TECHNICAL_TIMING_WEAK"
        if action is Action.BUY and require_catalyst_confirmation and (catalyst.availability is not CatalystAvailability.AVAILABLE or
                catalyst.catalyst_score is None or catalyst.catalyst_score<minimum_catalyst_score):
            action,reason_code=Action.HOLD,"CATALYST_CONFIRMATION_WEAK"
        if action is Action.BUY and not potential.available:
            action,reason_code=Action.HOLD,"POTENTIAL_ASSESSMENT_UNAVAILABLE"
        if action is Action.BUY and fallback and (potential.target_confidence is None or
                potential.target_confidence<fallback_minimum_potential_confidence):
            action,reason_code=Action.HOLD,"FUNDAMENTAL_FALLBACK_CONFIDENCE_TOO_LOW"
        rr_min=fallback_minimum_risk_reward_ratio if fallback else minimum_risk_reward_ratio
        if action is Action.BUY and (potential.entry_rr is None or potential.entry_rr<rr_min):
            action,reason_code=(Action.HOLD,"FUNDAMENTAL_FALLBACK_RR_TOO_LOW" if fallback else "INSUFFICIENT_RISK_REWARD")
        buy_tier: Literal["NORMAL","FLEX"] | None="NORMAL" if action is Action.BUY else None
        # FLEX is an explicit PAPER-only alternative to the normal gate. Red flags,
        # unavailable fundamentals/potential, and the universal RR floor remain blocking.
        if (action is not Action.BUY and paper_mode and not fundamental.blocks_entry and not fallback and
                potential.available and potential.entry_rr is not None and
                potential.entry_rr>=1.30 and fundamental.effective_score is not None and
                fundamental.effective_score>=40 and fundamental.confidence>=35 and
                technical.technical_score>=70):
            action,reason_code,buy_tier=Action.BUY,"FLEX_BUY_CANDIDATE","FLEX"
        # The FLEX floor is universal, but do not replace a more specific HOLD
        # reason produced by the unchanged normal/fallback gates.
        if action is Action.BUY and (potential.entry_rr is None or potential.entry_rr<1.30):
            action,reason_code,buy_tier=Action.HOLD,"RISK_REWARD_BELOW_FLEX_FLOOR",None
        position_size_multiplier=1.0 if buy_tier=="NORMAL" else .5 if buy_tier=="FLEX" else 0.0
        if buy_tier is not None:
            labels.append(f"BUY_TIER:{buy_tier}")
        # Preserve three dimensions; final score is presentation/ranking only, never an eligibility substitute.
        dimensions=[technical.technical_score]
        if potential.potential_score is not None: dimensions.append(potential.potential_score)
        if fundamental.fundamental_score is not None: dimensions.append(fundamental.fundamental_score)
        if catalyst.catalyst_score is not None: dimensions.append(catalyst.catalyst_score)
        final=sum(dimensions)/len(dimensions)
        reason=(f"{reason_code}: fundamental={fundamental.fundamental_score}, "
            f"effective_fundamental={fundamental.effective_score}, technical={technical.technical_score:.2f}, "
            f"catalyst={catalyst.catalyst_score}, RR={potential.entry_rr}; BUY still requires RiskEngine approval")
        if "FUNDAMENTAL_DATA_UNAVAILABLE_FALLBACK" in labels:
            reason=f"FUNDAMENTAL_DATA_UNAVAILABLE_FALLBACK; {reason}"
        return InvestmentDecision(symbol=technical.symbol,action=action,reason_code=reason_code,reason=reason,
            final_score=round(final,4),fundamental_score=fundamental.fundamental_score,
            technical_score=technical.technical_score,catalyst_score=catalyst.catalyst_score,
            potential_score=potential.potential_score,expected_target_price=potential.expected_target_price,
            expected_upside_pct=potential.expected_upside_pct,risk_reward_ratio=potential.risk_reward_ratio,
            target_1=potential.target_1,target_2=potential.target_2,target_3=potential.target_3,
            rr_t1=potential.rr_t1,rr_t2=potential.rr_t2,rr_t3=potential.rr_t3,entry_rr=potential.entry_rr,
            buy_tier=buy_tier,position_size_multiplier=position_size_multiplier,labels=labels)
