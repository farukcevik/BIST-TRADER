from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bistbot.app.config import FundamentalUnavailablePolicy, ScoringSettings
from bistbot.app.config import load_settings
from bistbot.app.models import (Action,MarketBar,MarketRegime,RiskDecision,RiskOutcome,
    RiskReasonCode,TechnicalSignal)
from bistbot.fundamental import (FinancialPeriod, FundamentalEngine, FundamentalProviderStatus,
    FundamentalSnapshot, Metric, UnavailableFundamentalProvider)
from bistbot.intelligence.catalyst import CatalystAssessment, CatalystAvailability
from bistbot.market.indicators import atr
from bistbot.app.runtime import BistBotApplication, _attach_potential_plan, _build_fundamental_engine
from bistbot.strategy.engine import DeterministicStrategyEngine
from bistbot.strategy.levels import BreakoutState, analyze_levels
from bistbot.strategy.potential import assess_potential


NOW=datetime(2026,1,1,tzinfo=timezone.utc)
def m(value): return Metric(value=value,available=True,source="fixture")


def test_missing_fundamentals_are_unknown_not_zero():
    snapshot=UnavailableFundamentalProvider().get_snapshot("AAA.IS",NOW)
    result=FundamentalEngine().evaluate(snapshot)
    assert result.fundamental_score is None and result.effective_score is None
    assert result.coverage==0 and result.confidence==0
    assert result.red_flags[0].code=="INSUFFICIENT_FUNDAMENTAL_DATA"
    assert not result.derived_metrics


def test_policy_config_is_explicit_and_preserves_buy_thresholds():
    assert load_settings("config.yaml").scoring.buy_threshold==72
    settings=load_settings("config.v1.yaml")
    assert settings.scoring.buy_threshold==65 and settings.strategy.minimum_risk_reward_ratio==1.5
    assert settings.fundamental.minimum_confidence==40
    assert settings.fundamental.unavailable_policy is FundamentalUnavailablePolicy.TECHNICAL_ONLY_FALLBACK
    assert settings.fundamental.fallback_minimum_risk_reward_ratio==2


def test_fundamental_score_renormalizes_available_components_and_flags_negative_equity():
    periods=[]
    for index in range(5):
        periods.append(FinancialPeriod(period_end=NOW-timedelta(days=90*index),revenue=m(140-index*10),
            ebitda=m(28-index*2),net_income=m(14-index),operating_cash_flow=m(16-index),free_cash_flow=m(8-index/2),
            net_debt=m(30),equity=m(-10 if index==0 else 80),total_assets=m(200),ebitda_margin=m(.2),net_margin=m(.1)))
    result=FundamentalEngine().evaluate(FundamentalSnapshot(symbol="AAA.IS",as_of=NOW,source="fixture",
        provider_status=FundamentalProviderStatus.PARTIAL,periods=periods))
    assert result.fundamental_score is not None
    assert result.effective_score is not None and result.effective_score < result.fundamental_score
    assert result.coverage == pytest.approx(len(result.available_metrics) / 26 * 100, abs=1e-4)
    assert "valuation_score" not in result.score_breakdown["renormalized_weights"]
    assert result.blocks_entry and any(flag.code=="NEGATIVE_EQUITY" for flag in result.red_flags)
    assert result.derived_metrics["yoy_revenue_growth"].available
    assert not result.derived_metrics["yoy_revenue_growth"].value < 0


def test_effective_score_calibrates_raw_score_without_scoring_unknown_as_zero():
    periods=[FinancialPeriod(period_end=NOW-timedelta(days=90*index),
        revenue=m(200-index*20),net_income=m(40-index*4),net_margin=m(.4))
        for index in range(5)]
    result=FundamentalEngine().evaluate(FundamentalSnapshot(symbol="TERA.IS",as_of=NOW,source="fixture",
        provider_status=FundamentalProviderStatus.PARTIAL,periods=periods))
    expected=result.fundamental_score*((result.coverage/100)*(result.confidence/100))**.5
    assert result.fundamental_score==100
    assert result.effective_score==pytest.approx(expected,abs=1e-4)
    assert result.effective_score < result.fundamental_score
    assert result.missing_metrics


def test_positive_equity_alone_is_neutral_not_perfect_balance_sheet():
    period=FinancialPeriod(period_end=NOW,equity=m(100))
    result=FundamentalEngine().evaluate(FundamentalSnapshot(symbol="AAA.IS",as_of=NOW,source="fixture",
        provider_status=FundamentalProviderStatus.PARTIAL,periods=[period]))
    assert result.score_breakdown["balance_sheet_score"]==50
    assert result.fundamental_score==50


def test_negative_baseline_growth_is_unavailable_and_margin_debt_trends_flagged():
    periods=[]
    for index in range(5):
        periods.append(FinancialPeriod(period_end=NOW-timedelta(days=90*index),revenue=m(10),ebitda=m(20),
            net_income=m(5 if index<4 else -5),operating_cash_flow=m(-1 if index==0 else 8),free_cash_flow=m(-2 if index==0 else 3),
            net_debt=m(60 if index==0 else 40),equity=m(80),total_assets=m(200),
            ebitda_margin=m(.10 if index==0 else .15),net_margin=m(.03 if index==0 else .08)))
    result=FundamentalEngine().evaluate(FundamentalSnapshot(symbol="AAA.IS",as_of=NOW,source="fixture",
        provider_status=FundamentalProviderStatus.PARTIAL,periods=periods))
    assert not result.derived_metrics["yoy_net_income_growth"].available
    codes={flag.code for flag in result.red_flags}
    assert {"NEGATIVE_OPERATING_CASH_FLOW","FREE_CASH_FLOW_WEAK","NET_DEBT_RISING","EBITDA_MARGIN_DECLINING","NET_MARGIN_DECLINING"}<=codes


@pytest.mark.parametrize(("multiple","status"),[(.6,"CHEAP"),(1.0,"FAIR"),(1.5,"EXTREME")])
def test_relative_valuation_bands_are_deterministic(multiple,status):
    period=FinancialPeriod(period_end=NOW,revenue=m(100),pe=m(10*multiple),pb=m(2*multiple),ev_ebitda=m(8*multiple),
        sector_pe=m(10),sector_pb=m(2),sector_ev_ebitda=m(8))
    snapshot=FundamentalSnapshot(symbol="AAA.IS",as_of=NOW,source="fixture",provider_status=FundamentalProviderStatus.PARTIAL,periods=[period])
    first=FundamentalEngine().evaluate(snapshot); second=FundamentalEngine().evaluate(snapshot)
    assert first==second and first.valuation_status.value==status


def bars(count=50):
    result=[]
    for i in range(count):
        base=100+i*.2+(2 if i%10==5 else 0)
        result.append(MarketBar(symbol="AAA.IS",timestamp=NOW+timedelta(days=i),open=base,high=base+1,low=base-1,close=base+.2,volume=1000))
    return result


def potential_levels(entry=110):
    return analyze_levels(bars()).model_copy(update={"support_1":entry-2,
        "resistance_1":entry+4,"resistance_2":entry+8,
        "swing_highs":[entry+4,entry+8,entry+20]})


def test_levels_and_potential_are_deterministic_and_no_lookahead():
    history=bars(); levels=analyze_levels(history)
    assert levels.timestamp==history[-1].timestamp
    levels=potential_levels(history[-1].close)
    potential=assess_potential(history[-1].close,history[-1].close-2,levels,1.5,70,65,1.5,75,70,60,MarketRegime.NORMAL)
    assert potential.expected_target_price>history[-1].close
    assert potential.risk_reward_ratio==potential.entry_rr
    assert potential.target_1 < potential.target_2 < potential.target_3
    assert potential.target_components["bounded_catalyst_extension"]==0


def test_levels_prefix_is_immune_to_future_bars_and_nearest_levels_are_ordered():
    prefix=bars(40); with_future=prefix+bars(10)[-10:]
    # Supplying the decision-time prefix gives an identical result regardless of later data held elsewhere.
    assert analyze_levels(prefix)==analyze_levels(with_future[:40])
    levels=analyze_levels(prefix)
    if levels.support_2: assert levels.support_1>=levels.support_2
    if levels.resistance_2: assert levels.resistance_1<=levels.resistance_2


@pytest.mark.parametrize("rr",[1.2,1.5,3.0])
def test_risk_reward_boundary_matrix(rr):
    levels=potential_levels()
    result=assess_potential(110,108,levels,1,70,65,1.5,75,70,60)
    result=result.model_copy(update={"risk_reward_ratio":rr,"entry_rr":rr})
    assert result.risk_reward_ratio==rr


def test_potential_confidence_responds_to_momentum_atr_and_fundamentals_but_target_ignores_catalyst():
    levels=potential_levels()
    low=assess_potential(110,108,levels,1,50,30,1,55,30,0)
    high=assess_potential(110,108,levels,2,80,90,2,90,90,100)
    catalyst_only=assess_potential(110,108,levels,1,50,30,1,55,30,100)
    assert high.target_confidence>low.target_confidence
    assert catalyst_only.expected_target_price==low.expected_target_price


def test_dynamic_stop_uses_structure_atr_floor_and_rejects_beyond_eight_percent():
    levels=potential_levels().model_copy(update={"support_1":106.5})
    result=assess_potential(110,108,levels,2,70,70,1.5,80,70,50)
    assert result.available and result.downside_reference==pytest.approx(106.0)
    assert result.downside_risk_pct==pytest.approx(100*4/110,rel=1e-3)
    too_deep=levels.model_copy(update={"support_1":99.0,"support_2":None,"swing_lows":[]})
    rejected=assess_potential(110,108,too_deep,2,70,70,1.5,80,70,50)
    assert not rejected.available and rejected.unavailable_reason=="REQUIRED_STOP_EXCEEDS_8_PERCENT"


def test_dynamic_stop_can_tighten_legacy_default_to_three_percent_floor():
    levels=potential_levels(100).model_copy(update={"support_1":99.0})
    result=assess_potential(100,95,levels,1,70,70,1.5,80,70,50)
    assert result.available and result.downside_reference==97
    assert result.downside_risk_pct==3


def test_approved_plan_metadata_contains_dynamic_stop_and_partial_targets():
    potential=assess_potential(110,105,potential_levels(),1,70,70,1.5,80,70,50)
    decision=RiskDecision(signal_id="00000000-0000-0000-0000-000000000001",
        outcome=RiskOutcome.APPROVE,reason_code=RiskReasonCode.APPROVED,reason="approved",
        approved_quantity=8)
    attached=_attach_potential_plan(decision,potential)
    assert attached.outcome is RiskOutcome.APPROVE and attached.approved_quantity==8
    assert attached.metadata["stop_price"]==potential.downside_reference
    assert attached.metadata["target_1"]==potential.target_1
    assert attached.metadata["target_2"]==potential.target_2
    assert attached.metadata["target_3"]==potential.target_3
    assert (attached.metadata["t1_exit_pct"],attached.metadata["t2_exit_pct"],
        attached.metadata["remaining_t3_or_trailing_pct"])==(25,25,50)


def test_two_validated_targets_required_and_third_target_is_optional():
    levels=potential_levels().model_copy(update={"resistance_2":None,"swing_highs":[114]})
    rejected=assess_potential(110,108,levels,1,70,70,1.5,80,70,50)
    assert not rejected.available and rejected.unavailable_reason=="INSUFFICIENT_VALIDATED_TARGETS"
    levels=levels.model_copy(update={"swing_highs":[114,118]})
    accepted=assess_potential(110,108,levels,1,70,70,1.5,80,70,50)
    assert accepted.available and accepted.target_1==114 and accepted.target_2==118
    assert accepted.target_3 is None and accepted.rr_t3 is None


def test_distant_third_target_cannot_inflate_entry_rr():
    near=potential_levels().model_copy(update={"swing_highs":[114,118,120]})
    distant=near.model_copy(update={"swing_highs":[114,118,1000]})
    first=assess_potential(110,108,near,1,70,70,1.5,80,70,50)
    second=assess_potential(110,108,distant,1,70,70,1.5,80,70,50)
    assert first.entry_rr==second.entry_rr
    assert first.rr_t3 < second.rr_t3
    assert first.risk_reward_ratio==first.entry_rr


def test_staged_strategy_has_explicit_risk_reward_reason():
    fundamental=FundamentalEngine().evaluate(UnavailableFundamentalProvider().get_snapshot("AAA.IS",NOW))
    technical=TechnicalSignal(symbol="AAA.IS",timestamp=NOW,technical_score=90,momentum_score=80,volume_score=80,
        trend_score=80,liquidity_score=80,volatility_score=70,overall_scanner_score=85,metrics={})
    levels=potential_levels()
    potential=assess_potential(110,108,levels,1,80,80,2,90,None,80)
    potential=potential.model_copy(update={"risk_reward_ratio":1.0,"entry_rr":1.0})
    catalyst=CatalystAssessment(catalyst_score=80,confidence=80,availability=CatalystAvailability.AVAILABLE)
    settings=ScoringSettings(technical=.3,momentum=.2,volume=.15,news_kap=.2,llm=.15,buy_threshold=65,sell_threshold=35)
    decision=DeterministicStrategyEngine(settings).evaluate_investment(technical,fundamental,catalyst,potential,
        unavailable_policy=FundamentalUnavailablePolicy.TECHNICAL_ONLY_FALLBACK,
        fallback_minimum_potential_confidence=0)
    assert decision.action is Action.HOLD and decision.reason_code=="FUNDAMENTAL_FALLBACK_RR_TOO_LOW"


@pytest.mark.parametrize(("fundamental_score","confidence","catalyst_score","expected"),[
    (80,80,80,"BUY_CANDIDATE"),(80,20,80,"FUNDAMENTAL_CONFIDENCE_LOW"),
    (80,80,20,"CATALYST_CONFIRMATION_WEAK"),(30,80,80,"FUNDAMENTAL_QUALITY_LOW")])
def test_staged_quality_timing_catalyst_matrix(fundamental_score,confidence,catalyst_score,expected):
    base=FundamentalEngine().evaluate(UnavailableFundamentalProvider().get_snapshot("AAA.IS",NOW))
    fundamental=base.model_copy(update={"provider_status":FundamentalProviderStatus.AVAILABLE,
        "fundamental_score":fundamental_score,"effective_score":fundamental_score,"confidence":confidence})
    technical=TechnicalSignal(symbol="AAA.IS",timestamp=NOW,technical_score=90,momentum_score=80,volume_score=80,
        trend_score=80,liquidity_score=80,volatility_score=70,overall_scanner_score=85,metrics={})
    potential=assess_potential(110,108,potential_levels(),1,80,80,2,90,fundamental_score,catalyst_score).model_copy(update={"risk_reward_ratio":3,"entry_rr":3})
    catalyst=CatalystAssessment(catalyst_score=catalyst_score,confidence=80,availability=CatalystAvailability.AVAILABLE)
    engine=DeterministicStrategyEngine(ScoringSettings(technical=.3,momentum=.2,volume=.15,news_kap=.2,llm=.15,buy_threshold=65,sell_threshold=35))
    assert engine.evaluate_investment(technical,fundamental,catalyst,potential).reason_code==expected


@pytest.mark.parametrize(("score","confidence","expected"),[
    (80,20,"FUNDAMENTAL_CONFIDENCE_LOW"),
    (30,80,"FUNDAMENTAL_QUALITY_LOW")])
def test_partial_fundamentals_keep_quality_and_confidence_gates(score,confidence,expected):
    technical,fundamental,catalyst,potential=_fallback_inputs()
    if expected=="FUNDAMENTAL_QUALITY_LOW":
        technical=technical.model_copy(update={"technical_score":79})
    fundamental=fundamental.model_copy(update={"provider_status":FundamentalProviderStatus.PARTIAL,
        "fundamental_score":score,"effective_score":score,"confidence":confidence,
        "missing_metrics":["free_cash_flow"]})
    decision=_engine().evaluate_investment(technical,fundamental,catalyst,potential)
    assert decision.action is Action.HOLD and decision.reason_code==expected
    assert "FUNDAMENTAL_DATA_PARTIAL" in decision.labels
    assert "MISSING_FUNDAMENTAL_METRIC:free_cash_flow" in decision.labels


def _fallback_inputs(*,technical_score=90,rr=3,target_confidence=80):
    fundamental=FundamentalEngine().evaluate(UnavailableFundamentalProvider().get_snapshot("AAA.IS",NOW))
    technical=TechnicalSignal(symbol="AAA.IS",timestamp=NOW,technical_score=technical_score,momentum_score=80,
        volume_score=80,trend_score=80,liquidity_score=80,volatility_score=70,overall_scanner_score=85,metrics={})
    potential=assess_potential(110,108,potential_levels(),1,80,80,2,technical_score,None,80).model_copy(
        update={"risk_reward_ratio":rr,"entry_rr":rr,"target_confidence":target_confidence})
    catalyst=CatalystAssessment(catalyst_score=80,confidence=80,availability=CatalystAvailability.AVAILABLE)
    return technical,fundamental,catalyst,potential


def _engine():
    return DeterministicStrategyEngine(ScoringSettings(technical=.3,momentum=.2,volume=.15,news_kap=.2,llm=.15,
        buy_threshold=65,sell_threshold=35))


def test_unavailable_fundamental_block_policy_holds_without_fabricating_score():
    decision=_engine().evaluate_investment(*_fallback_inputs(),unavailable_policy=FundamentalUnavailablePolicy.BLOCK)
    assert decision.action is Action.HOLD and decision.reason_code=="FUNDAMENTAL_DATA_UNAVAILABLE"
    assert decision.fundamental_score is None


def test_effective_fundamental_score_calibrates_buy_gate_without_changing_raw_score():
    technical,fundamental,catalyst,potential=_fallback_inputs()
    fundamental=fundamental.model_copy(update={"provider_status":FundamentalProviderStatus.PARTIAL,
        "fundamental_score":100,"effective_score":45,"coverage":40,"confidence":80})
    technical=technical.model_copy(update={"technical_score":79})
    decision=_engine().evaluate_investment(technical,fundamental,catalyst,potential)
    assert decision.action is Action.HOLD and decision.reason_code=="FUNDAMENTAL_QUALITY_LOW"
    assert decision.fundamental_score==100
    assert "effective_fundamental=45" in decision.reason


def test_missing_effective_score_uses_unavailable_policy_without_fabricating_zero():
    technical,fundamental,catalyst,potential=_fallback_inputs()
    fundamental=fundamental.model_copy(update={"provider_status":FundamentalProviderStatus.PARTIAL,
        "fundamental_score":100,"effective_score":None,"coverage":40,"confidence":80})
    blocked=_engine().evaluate_investment(technical,fundamental,catalyst,potential,
        unavailable_policy=FundamentalUnavailablePolicy.BLOCK)
    fallback=_engine().evaluate_investment(technical,fundamental,catalyst,potential,
        unavailable_policy=FundamentalUnavailablePolicy.TECHNICAL_ONLY_FALLBACK)
    assert blocked.action is Action.HOLD and blocked.reason_code=="FUNDAMENTAL_DATA_UNAVAILABLE"
    assert fallback.action is Action.BUY
    assert blocked.fundamental_score==100 and "effective_fundamental=None" in blocked.reason


@pytest.mark.parametrize(("overrides","reason"),[
    ({"technical_score":74},"FUNDAMENTAL_FALLBACK_TECHNICAL_SCORE_TOO_LOW"),
    ({"rr":1.99},"FUNDAMENTAL_FALLBACK_RR_TOO_LOW"),
    ({"target_confidence":64.99},"FUNDAMENTAL_FALLBACK_CONFIDENCE_TOO_LOW")])
def test_technical_fallback_has_conservative_configurable_gates(overrides,reason):
    decision=_engine().evaluate_investment(*_fallback_inputs(**overrides),
        unavailable_policy=FundamentalUnavailablePolicy.TECHNICAL_ONLY_FALLBACK)
    assert decision.action is Action.HOLD and decision.reason_code==reason
    assert decision.fundamental_score is None


def test_technical_fallback_continues_when_all_gates_pass():
    decision=_engine().evaluate_investment(*_fallback_inputs(),
        unavailable_policy=FundamentalUnavailablePolicy.TECHNICAL_ONLY_FALLBACK)
    assert decision.action is Action.BUY
    assert "FUNDAMENTAL_DATA_UNAVAILABLE_FALLBACK" in decision.labels
    assert decision.fundamental_score is None


def _flex_inputs(*,effective=40,confidence=35,technical_score=70,rr=1.30):
    technical,fundamental,catalyst,potential=_fallback_inputs(technical_score=technical_score,rr=rr)
    fundamental=fundamental.model_copy(update={"provider_status":FundamentalProviderStatus.AVAILABLE,
        "fundamental_score":effective,"effective_score":effective,"confidence":confidence})
    return technical,fundamental,catalyst,potential


def test_normal_buy_keeps_existing_rules_and_full_size_tier():
    decision=_engine().evaluate_investment(*_flex_inputs(effective=80,confidence=80,technical_score=90,rr=2))
    assert decision.action is Action.BUY and decision.reason_code=="BUY_CANDIDATE"
    assert decision.buy_tier=="NORMAL" and decision.position_size_multiplier==1
    assert "BUY_TIER:NORMAL" in decision.labels


def test_flex_buy_is_paper_only_and_half_size():
    inputs=_flex_inputs()
    default_safe=_engine().evaluate_investment(*inputs)
    paper=_engine().evaluate_investment(*inputs,paper_mode=True)
    assert default_safe.action is Action.HOLD and default_safe.buy_tier is None
    assert paper.action is Action.BUY and paper.reason_code=="FLEX_BUY_CANDIDATE"
    assert paper.buy_tier=="FLEX" and paper.position_size_multiplier==.5
    assert "BUY_TIER:FLEX" in paper.labels


def test_runtime_investment_decision_enables_flex_for_configured_paper_mode():
    application=BistBotApplication.__new__(BistBotApplication)
    application.settings=load_settings("config.yaml")
    application.strategy=_engine()
    decision=application._investment_decision(*_flex_inputs())
    assert application.settings.trading_mode=="PAPER"
    assert decision.action is Action.BUY
    assert decision.buy_tier=="FLEX" and decision.position_size_multiplier==.5


@pytest.mark.parametrize(("overrides"),[
    {"rr":1.2999},{"effective":39.99},{"confidence":34.99},{"technical_score":69.99}])
def test_flex_thresholds_are_hard_boundaries(overrides):
    decision=_engine().evaluate_investment(*_flex_inputs(**overrides),paper_mode=True)
    assert decision.action is Action.HOLD and decision.buy_tier is None
    assert decision.position_size_multiplier==0


def test_rr_below_flex_floor_always_holds_even_if_normal_rr_is_configured_lower():
    decision=_engine().evaluate_investment(*_flex_inputs(effective=80,confidence=80,technical_score=90,rr=1.29),
        minimum_risk_reward_ratio=1.0,paper_mode=True)
    assert decision.action is Action.HOLD and decision.reason_code=="RISK_REWARD_BELOW_FLEX_FLOOR"
    assert decision.buy_tier is None and decision.position_size_multiplier==0


def test_flex_does_not_bypass_fundamental_red_flags_or_missing_potential():
    technical,fundamental,catalyst,potential=_flex_inputs()
    blocked=fundamental.model_copy(update={"blocks_entry":True})
    unavailable=potential.model_copy(update={"available":False})
    red_flag_decision=_engine().evaluate_investment(
        technical,blocked,catalyst,potential,paper_mode=True)
    unavailable_decision=_engine().evaluate_investment(
        technical,fundamental,catalyst,unavailable,paper_mode=True)
    assert red_flag_decision.action is Action.HOLD
    assert red_flag_decision.reason_code=="FUNDAMENTAL_BLOCKING_RED_FLAG"
    assert unavailable_decision.action is Action.HOLD
    assert unavailable_decision.buy_tier is None


def test_missing_fundamental_does_not_inject_neutral_potential_confidence():
    levels=potential_levels()
    missing=assess_potential(110,108,levels,1,80,80,2,90,None,80)
    explicit_zero=assess_potential(110,108,levels,1,80,80,2,90,0,80)
    neutral=assess_potential(110,108,levels,1,80,80,2,90,50,80)
    assert missing.target_confidence==explicit_zero.target_confidence
    assert missing.target_confidence<neutral.target_confidence


@pytest.mark.parametrize("entry",[None,0,-1,float("nan")])
def test_invalid_entry_makes_all_potential_price_and_risk_fields_unavailable(entry):
    result=assess_potential(entry,100,analyze_levels(bars()),1,80,80,2,90,None,80)
    assert not result.available and result.unavailable_reason=="ENTRY_PRICE_UNAVAILABLE"
    assert result.target_confidence>0
    assert all(getattr(result,name) is None for name in ("potential_score","expected_target_price",
        "expected_upside_pct","downside_reference","downside_risk_pct","risk_reward_ratio"))


def test_near_identical_levels_are_deduplicated_with_atr_tick_epsilon():
    history=bars()
    levels=analyze_levels(history,dedup_atr_fraction=.1,tick_size=.05)
    atr_value=atr([bar.high for bar in history],[bar.low for bar in history],[bar.close for bar in history],14)
    epsilon=max(.05,atr_value*.1)
    for values in (levels.swing_highs,levels.swing_lows):
        assert all(abs(left-right)>epsilon for index,left in enumerate(values) for right in values[index+1:])


def test_runtime_uses_documented_strategy_risk_reward_setting():
    settings=load_settings("config.yaml")
    settings.strategy.minimum_risk_reward_ratio=2.75
    settings.fundamental.minimum_score=67
    captured={}
    class StrategySpy:
        def evaluate_investment(self,*args,**kwargs): captured.update(kwargs)
    app=SimpleNamespace(settings=settings,strategy=StrategySpy())
    BistBotApplication._investment_decision(app,None,None,None,None)
    assert captured["minimum_risk_reward_ratio"]==2.75
    assert captured["minimum_fundamental_score"]==67
    assert "minimum_risk_reward_ratio" not in type(settings.investment_policy).model_fields
    assert "minimum_fundamental_score" not in type(settings.investment_policy).model_fields


def test_runtime_fundamental_engine_consumes_documented_weights():
    periods=[]
    for index in range(5):
        periods.append(FinancialPeriod(period_end=NOW-timedelta(days=90*index),revenue=m(200-index*20),
            ebitda=m(20-index),net_income=m(5),operating_cash_flow=m(5),free_cash_flow=m(1),
            net_debt=m(200),equity=m(20),total_assets=m(100),ebitda_margin=m(.1),net_margin=m(.025)))
    snapshot=FundamentalSnapshot(symbol="AAA.IS",as_of=NOW,source="fixture",
        provider_status=FundamentalProviderStatus.AVAILABLE,periods=periods)
    settings=load_settings("config.yaml")
    growth_weights={name:(1.0 if name=="growth_score" else 0.0) for name in settings.fundamental.weights}
    balance_weights={name:(1.0 if name=="balance_sheet_score" else 0.0) for name in settings.fundamental.weights}
    settings.fundamental.weights=growth_weights
    growth_score=_build_fundamental_engine(settings).evaluate(snapshot).fundamental_score
    settings.fundamental.weights=balance_weights
    balance_score=_build_fundamental_engine(settings).evaluate(snapshot).fundamental_score
    assert growth_score!=balance_score
