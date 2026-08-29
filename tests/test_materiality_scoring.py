from __future__ import annotations

from datetime import datetime,timezone

from bistbot.app.config import load_settings
from bistbot.app.models import Action,EventSourceType,IntelligenceStatus,LLMAnalysis,LLMStatus,RankedEventCandidate,TechnicalSignal
from bistbot.intelligence.materiality import classify_event
from bistbot.intelligence.ranking import make_event
from bistbot.strategy.engine import DeterministicStrategyEngine

NOW=datetime(2026,8,21,12,tzinfo=timezone.utc)


def event(title,body="KAMUYU AYDINLATMA PLATFORMU"):
    return make_event(symbol="GUBRF.IS",source="KAP",source_type=EventSourceType.KAP,title=title,body=body,
        published_at=NOW,fetched_at=NOW,trust_score=95,source_id=title)


def test_administrative_kap_is_reliable_but_low_materiality():
    circuit=classify_event(event("GUBRF.E işlem sırasında Pay Bazında Devre Kesici Uygulaması devreye girmiştir"))
    routine=classify_event(event("DÖNÜŞÜM","MERKEZİ KAYIT KURULUŞU A.Ş."))
    assert circuit.event_type=="CIRCUIT_BREAKER" and circuit.materiality_score<50
    assert routine.event_type=="ROUTINE_MKK_NOTICE" and routine.materiality_score<50
    assert circuit.source_reliability==95 and circuit.verification=="PRIMARY_CONFIRMED"


def test_material_contract_is_eligible_but_title_only_is_capped():
    detailed=classify_event(event("Yeni sözleşme","Şirket TRY 2 milyar tutarında bağlayıcı sözleşme imzaladı."))
    title_only=classify_event(event("Yeni sözleşme",""))
    assert detailed.event_type=="CONTRACT" and detailed.materiality_score>=50
    assert title_only.materiality_score<50


def test_score_breakdown_reconciles_and_neutral_missing_intelligence_is_renormalized():
    signal=TechnicalSignal(symbol="GUBRF.IS",timestamp=NOW,technical_score=68.8846,momentum_score=56.5269,
        volume_score=96.1111,trend_score=59.5585,liquidity_score=78.1948,volatility_score=60,
        overall_scanner_score=74.9131,metrics={},reasons=[])
    ranking=RankedEventCandidate(symbol="GUBRF.IS",scanner_score=74.9131,event_score=50,combined_score=74.9131,
        intelligence_status=IntelligenceStatus.NO_NEWS)
    analysis=LLMAnalysis(symbol="GUBRF.IS",sentiment=0,importance=0,catalyst_score=0,priced_in_probability=50,
        risk_score=50,confidence=0,time_horizon="none",action_bias=Action.HOLD,summary="not required",
        bull_case="",bear_case="",llm_status=LLMStatus.NOT_REQUIRED)
    decision=DeterministicStrategyEngine(load_settings("config.yaml").scoring).evaluate(signal,ranking,analysis)
    breakdown=decision.score_breakdown
    assert abs(sum(breakdown["contributions"].values())-decision.final_score)<.01
    assert {"news_kap","llm"}<=set(breakdown["missing_components"])
    assert {"trend","liquidity"}.isdisjoint(breakdown["missing_components"])
    assert breakdown["contributions"]["trend"]>0 and breakdown["contributions"]["liquidity"]>0
