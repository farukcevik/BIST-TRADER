from datetime import datetime,timedelta,timezone
import json

from bistbot.app.config import MarketRegimeSettings
from bistbot.app.config import load_settings
from bistbot.app.runtime import BistBotApplication
from bistbot.app.models import (Action,MacroEvent,MacroRiskCategory,MarketRegime,MarketRegimeState,TimestampSource,
    LLMCompletion,SignalMode,StrategyDecision)
from bistbot.market_regime.engine import MarketRegimeEngine,apply_market_overlay
from bistbot.market_regime.materiality import classify_macro_event,freshness_weight
from bistbot.market_regime.llm import MacroLLMAnalyst
from bistbot.market_regime.provider import RealMacroNewsProvider
from bistbot.broker.paper import PaperBroker
from bistbot.notifications.base import SafeNotificationDispatcher
from bistbot.storage.database import Database
from bistbot.storage.repositories import MarketRegimeRepository

NOW=datetime(2026,8,28,12,tzinfo=timezone.utc)

def macro(title,*,event_id="one",category=MacroRiskCategory.FX,negative=None):
    return MacroEvent(canonical_event_id=event_id,source_id=event_id,source="TCMB",title=title,
        published_at=NOW,fetched_at=NOW,category=category,source_reliability=100,
        negative_sectors=negative or [])

def sourced(title,source="TCMB",published=NOW,event_id="event",timestamp_source=TimestampSource.SOURCE):
    return MacroEvent(canonical_event_id=event_id,source_id=event_id,source=source,title=title,
        published_at=published,fetched_at=NOW,timestamp_source=timestamp_source,
        category=MacroRiskCategory.TURKEY_MACRO,source_reliability=98)

def test_materiality_is_deterministic_and_routine_news_stays_below_llm_gate():
    assert classify_macro_event(macro("Unexpected rate decision")).materiality=="HIGH"
    assert classify_macro_event(macro("Routine meeting schedule commentary")).materiality=="LOW"

def test_regime_and_sector_overlay_are_separate():
    engine=MarketRegimeEngine(MarketRegimeSettings(),now=lambda:NOW)
    state=engine.refresh([macro("USDTRY surged in a currency crash",negative=["airlines"])])
    overlay=engine.overlay("THYAO",state)
    assert state.regime is MarketRegime.RISK_OFF
    assert overlay.market_adjustment==-10 and overlay.sector_adjustment==-2

def test_crisis_blocks_new_entry_and_exposes_decision_contract():
    engine=MarketRegimeEngine(MarketRegimeSettings(),now=lambda:NOW)
    state=MarketRegimeState(regime=MarketRegime.CRISIS,market_risk_score=95,confidence=90,last_updated=NOW)
    base=StrategyDecision(symbol="ASELS.IS",action=Action.BUY,final_score=82,reason="base",
        signal_mode=SignalMode.TECHNICAL_ONLY)
    result=apply_market_overlay(base,engine.overlay("ASELS",state),72)
    assert result.action is Action.HOLD
    assert result.score_breakdown=={
        "base_stock_score":82,"market_regime":"CRISIS","market_adjustment":0,
        "sector_adjustment":0,"adjusted_final_score":82,"adjusted_buy_threshold":72,
        "position_multiplier":0,"block_new_entries":True}

def test_unchanged_canonical_event_is_cached_once():
    engine=MarketRegimeEngine(MarketRegimeSettings(),now=lambda:NOW)
    event=macro("USDTRY surged in a currency crash")
    engine.refresh([event]); engine.refresh([event])
    assert len(engine._events)==1

def test_material_positive_shock_can_produce_risk_on():
    engine=MarketRegimeEngine(MarketRegimeSettings(),now=lambda:NOW)
    state=engine.refresh([macro("Sovereign rating upgrade",category=MacroRiskCategory.TURKEY_MACRO)])
    assert state.regime is MarketRegime.RISK_ON and state.market_risk_score==0

def test_normal_is_identity_and_caution_changes_score_and_threshold():
    engine=MarketRegimeEngine(MarketRegimeSettings(),now=lambda:NOW)
    base=StrategyDecision(symbol="ASELS.IS",action=Action.BUY,final_score=80,reason="base")
    normal=apply_market_overlay(base,engine.overlay("ASELS",MarketRegimeState(regime="NORMAL",last_updated=NOW)),72)
    caution=apply_market_overlay(base,engine.overlay("ASELS",MarketRegimeState(regime="CAUTION",last_updated=NOW)),72)
    assert normal.final_score==80 and normal.score_breakdown["adjusted_buy_threshold"]==72
    assert caution.final_score==76 and caution.score_breakdown["adjusted_buy_threshold"]==75

def test_risk_off_position_multiplier_and_unmapped_sector_is_neutral():
    engine=MarketRegimeEngine(MarketRegimeSettings(),now=lambda:NOW)
    state=MarketRegimeState(regime="RISK_OFF",negative_sectors=["airlines"],last_updated=NOW)
    assert engine.overlay("THYAO",state).sector_adjustment==-2
    assert engine.overlay("UNKNOWN",state).sector_adjustment==0
    assert engine.overlay("UNKNOWN",state).position_multiplier==.4

class Response:
    def __init__(self,payload): self.payload=payload
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def read(self): return self.payload

def test_real_provider_returns_official_material_event_and_real_status():
    rss=("<rss><channel><item><title>Unexpected rate decision</title>"
         "<link>https://official.example/decision</link><pubDate>Fri, 28 Aug 2026 12:00:00 GMT</pubDate>"
         "</item></channel></rss>").encode()
    market={"chart":{"result":[{"timestamp":[1,2],"indicators":{"quote":[{"close":[100,100]}]}}]}}
    def opener(request,**kwargs):
        return Response(json.dumps(market).encode() if "finance/chart" in request.full_url else rss)
    provider=RealMacroNewsProvider(opener=opener,now=lambda:NOW)
    events=provider.fetch(); classified=[classify_macro_event(event) for event in events]
    assert provider.status=="PARTIAL"
    assert any(event.materiality=="HIGH" and event.source=="TCMB" for event in classified)

def test_normal_paper_runtime_never_defaults_to_static(tmp_path):
    settings=load_settings("config.v1.yaml"); database=Database(str(tmp_path/"paper.db"))
    try:
        broker=PaperBroker(settings.capital,settings.execution.commission_pct,settings.execution.slippage_pct,
            database=database,risk_settings=settings.risk)
        app=BistBotApplication(settings,database,broker,SafeNotificationDispatcher())
        assert isinstance(app.macro_provider,RealMacroNewsProvider)
    finally: database.close()

def test_macro_llm_receives_only_high_events_and_cache_prevents_repeat(tmp_path):
    class Provider:
        provider_mode="REAL"
        def __init__(self): self.calls=0
        def complete(self,**kwargs):
            self.calls+=1
            return LLMCompletion(model_name="mock",content=json.dumps({"regime":"RISK_OFF",
                "market_risk_score":80,"confidence":90,"event_summary":"Verified shock",
                "expected_market_direction":"NEGATIVE","expected_duration":"days",
                "affected_sectors":[],"positive_sectors":[],"negative_sectors":[],
                "uncertainty":"evolving","source_ids":["one"]}))
    database=Database(str(tmp_path/"macro.db")); provider=Provider()
    try:
        analyst=MacroLLMAnalyst(provider,MarketRegimeRepository(database),model_name="mock",threshold=70)
        high=classify_macro_event(macro("USDTRY surged in a currency crash"))
        low=classify_macro_event(macro("Routine meeting schedule commentary",event_id="low"))
        assert analyst.analyze([high,low]) is not None
        assert analyst.analyze([high,low]) is not None
        assert provider.calls==1 and analyst.last_analyzed_ids=={"one"}
    finally: database.close()

def test_tcmb_rate_decision_scores_far_above_essay_competition():
    rate=classify_macro_event(sourced("Faiz Oranlarına İlişkin Basın Duyurusu"),now=NOW)
    essay=classify_macro_event(sourced("Merkez Bankası Üniversite Öğrencileri Makale Yarışması Sonucu"),now=NOW)
    assert rate.event_type=="CENTRAL_BANK_RATE_DECISION" and rate.materiality_score==95
    assert essay.event_type=="ACADEMIC_CEREMONIAL" and essay.materiality_score<=10

def test_fomc_statement_scores_far_above_ceremonial_public_event():
    fomc=classify_macro_event(sourced("Federal Reserve issues FOMC statement",source="Federal Reserve"),now=NOW)
    concert=classify_macro_event(sourced("ECB invites the public to Open Air concert",source="ECB"),now=NOW)
    assert fomc.materiality_score>=90 and concert.materiality_score<=5

def test_macroprudential_policy_is_high_materiality():
    event=classify_macro_event(sourced("Makroihtiyati Çerçeveye İlişkin Basın Duyurusu"),now=NOW)
    assert event.event_type=="MACROPRUDENTIAL_POLICY" and event.materiality_score>=80

def test_old_events_decay_and_cannot_create_current_crisis():
    old=sourced("FX capital control policy",published=NOW-timedelta(days=30))
    classified=classify_macro_event(old,now=NOW)
    assert classified.freshness_weight==.05 and classified.effective_materiality<10
    state=MarketRegimeEngine(MarketRegimeSettings(),now=lambda:NOW).refresh([old])
    assert state.regime is MarketRegime.NORMAL and state.market_risk_score==0

def test_fetch_timestamp_fallback_is_not_fresh_source_time():
    fallback=sourced("Faiz Oranlarına İlişkin Basın Duyurusu",timestamp_source=TimestampSource.FETCH_FALLBACK)
    classified=classify_macro_event(fallback,now=NOW)
    assert classified.published_at==NOW and classified.timestamp_source is TimestampSource.FETCH_FALLBACK
    assert classified.freshness_weight==0 and classified.effective_materiality==0

def test_fresh_rate_decision_above_threshold_can_reach_macro_llm(tmp_path):
    class Provider:
        provider_mode="REAL"
        def __init__(self):self.calls=0
        def complete(self,**kwargs):
            self.calls+=1
            return LLMCompletion(model_name="mock",content=json.dumps({"regime":"RISK_OFF","market_risk_score":70,
                "confidence":80,"event_summary":"Rate decision","expected_market_direction":"MIXED",
                "expected_duration":"days","affected_sectors":[],"positive_sectors":[],"negative_sectors":[],
                "uncertainty":"path dependent","source_ids":["rate"]}))
    database=Database(str(tmp_path/"rate.db")); provider=Provider()
    try:
        event=classify_macro_event(sourced("Faiz Oranlarına İlişkin Basın Duyurusu",event_id="rate"),now=NOW)
        result=MacroLLMAnalyst(provider,MarketRegimeRepository(database),model_name="mock",threshold=70).analyze([event])
        assert result is not None and provider.calls==1
    finally:database.close()
