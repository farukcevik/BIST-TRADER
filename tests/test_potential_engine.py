from datetime import datetime, timedelta, timezone

import pytest

from bistbot.app.config import ScoringSettings
from bistbot.app.models import (
    Action, EntryPlan, IntelligenceStatus, LLMAnalysis, LLMStatus, MarketBar, MarketRegime,
    RankedEventCandidate, TechnicalSignal,
)
from bistbot.strategy.engine import DeterministicStrategyEngine
from bistbot.strategy.potential import PotentialEngine, PotentialInput

NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)


def bars(*, resistance=112.0, volatility=1.0):
    result=[]
    for i in range(30):
        close=96 + i * 0.14
        high=close + volatility
        if i == 20: high=resistance
        result.append(MarketBar(symbol="AAA",timestamp=NOW-timedelta(hours=30-i),
            open=close-.2,high=max(high,close),low=close-volatility,close=close,volume=1000+i*10))
    return result


def technical(*, strength=70, atr=2.0, relative_volume=1.5):
    return TechnicalSignal(symbol="AAA",timestamp=NOW,technical_score=strength,momentum_score=strength,
        volume_score=strength,trend_score=strength,liquidity_score=80,volatility_score=70,
        overall_scanner_score=strength,metrics={"latest_price":100.0,"atr_14":atr,"ema_9":101,
            "ema_21":99,"relative_volume":relative_volume},reasons=[])


def evaluate(**kwargs):
    return PotentialEngine().evaluate(PotentialInput(technical=kwargs.pop("signal",technical()),
        bars=kwargs.pop("history",bars()),decision_timestamp=NOW,**kwargs))


def test_output_is_deterministic_auditable_and_ordered():
    first=evaluate(catalyst_score=80,catalyst_confidence=50)
    second=evaluate(catalyst_score=80,catalyst_confidence=50)
    assert first == second
    assert first.entry_price < first.target_1 <= first.target_2 <= first.target_3
    assert first.target_components.bars_used == 30
    assert first.target_components.catalyst_adjustment_pct <= .015


def test_future_data_is_rejected_instead_of_silently_used():
    history=bars()
    history[-1]=history[-1].model_copy(update={"timestamp":NOW+timedelta(seconds=1)})
    with pytest.raises(ValueError,match="future bars"):
        evaluate(history=history)


def test_stop_is_volatility_aware_and_bounded():
    low=evaluate(signal=technical(atr=.7))
    high=evaluate(signal=technical(atr=7.0))
    assert .03 <= low.downside_risk_pct <= .08
    assert .03 <= high.downside_risk_pct <= .08
    assert high.downside_risk_pct > low.downside_risk_pct


def test_near_resistance_reduces_target_against_far_resistance():
    near=evaluate(history=bars(resistance=101.0))
    far=evaluate(history=bars(resistance=120.0))
    assert near.target_2 < far.target_2


def test_strong_momentum_and_volume_raise_potential_and_target():
    weak=evaluate(signal=technical(strength=35,relative_volume=.5))
    strong=evaluate(signal=technical(strength=90,relative_volume=2.5))
    assert strong.potential_score > weak.potential_score
    assert strong.target_3 > weak.target_3


def scoring():
    return ScoringSettings(technical=.3,momentum=.2,volume=.15,news_kap=.2,llm=.15,
        buy_threshold=65,sell_threshold=35,minimum_risk_reward_ratio=1.5)


def decision(plan):
    signal=technical(strength=90)
    ranking=RankedEventCandidate(symbol="AAA",scanner_score=90,event_score=50,combined_score=90,
        intelligence_status=IntelligenceStatus.NO_NEWS)
    analysis=LLMAnalysis(symbol="AAA",sentiment=0,importance=0,catalyst_score=0,priced_in_probability=0,
        risk_score=0,confidence=0,time_horizon="none",action_bias=Action.BUY,summary="",bull_case="",
        bear_case="",llm_status=LLMStatus.NOT_REQUIRED)
    return DeterministicStrategyEngine(scoring()).evaluate(signal,ranking,analysis,entry_plan=plan)


def plan_with_rr(rr):
    source=evaluate()
    return source.model_copy(update={"risk_reward_ratio":rr})


@pytest.mark.parametrize("rr,action,reason",[(1.2,Action.HOLD,"INSUFFICIENT_RISK_REWARD"),
    (1.5,Action.BUY,None),(3.0,Action.BUY,None)])
def test_risk_reward_gate_has_inclusive_boundary(rr,action,reason):
    result=decision(plan_with_rr(rr))
    assert result.action is action
    if reason: assert result.reason == reason


def test_revaluation_is_bounded_and_never_loosens_stop():
    engine=PotentialEngine(); original=evaluate(signal=technical(strength=90))
    weak=technical(strength=40)
    evidence=PotentialInput(technical=weak,bars=bars(),decision_timestamp=NOW,
        market_regime=MarketRegime.CAUTION)
    revised=engine.re_evaluate(original,evidence,"TREND_DETERIORATION")
    assert revised.initial_stop_price>=original.initial_stop_price
    assert revised.target_1>=original.target_1*(1-engine.settings.target_revision_max_down_pct)
    assert revised.target_3<=original.target_3*(1+engine.settings.target_revision_max_up_pct)
    assert revised.target_method.endswith("REVALUATED:TREND_DETERIORATION")
