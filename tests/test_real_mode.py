from __future__ import annotations

import io,json
from datetime import datetime,timezone

from bistbot.app.config import load_settings
from bistbot.app.models import (Action,IntelligenceStatus,LLMAnalysisInput,LLMStatus,RankedEventCandidate,
                                TechnicalSignal)
from bistbot.intelligence.llm_provider import hold_analysis
from bistbot.intelligence.kap_provider import RealKapProvider
from bistbot.intelligence.news_provider import YahooFinanceNewsProvider
from bistbot.market.provider import BistSymbolUniverse,YahooChartTransport,_is_stale_for_bist_session
from datetime import timedelta
from bistbot.strategy.engine import DeterministicStrategyEngine


def technical(symbol="THYAO.IS",score=90):
    return TechnicalSignal(symbol=symbol,timestamp=datetime.now(timezone.utc),technical_score=score,
        momentum_score=score,volume_score=score,trend_score=score,liquidity_score=75,volatility_score=60,
        overall_scanner_score=score,metrics={},reasons=[])


def test_maintained_universe_contains_real_tickers_only():
    universe=BistSymbolUniverse(opener=lambda *args,**kwargs: (_ for _ in ()).throw(OSError("offline"))); symbols=universe.load()
    assert len(symbols)>=100 and not universe.invalid_symbols
    assert {"THYAO.IS","ASELS.IS","TUPRS.IS","EREGL.IS","CWENE.IS","MAGEN.IS"}<=set(symbols)
    assert not any(symbol.startswith("BIST") and symbol[4:7].isdigit() for symbol in symbols)


def test_kap_active_company_universe_is_normalized_without_invention():
    payload=[{"stockCode":"THYAO","payIslemDurumu":"1"},{"stockCode":"ASELS, TUPRS","payIslemDurumu":"1"},
             {"stockCode":"bad-symbol","payIslemDurumu":"1"},{"stockCode":"NOTTRADED","payIslemDurumu":"0"}]
    universe=BistSymbolUniverse(opener=lambda request,timeout:io.BytesIO(json.dumps(payload).encode()))
    assert universe.load()==["THYAO.IS","ASELS.IS","TUPRS.IS"]
    assert universe.configured_count==4 and universe.invalid_symbols==["BAD-SYMBOL"]


def test_real_kap_provider_maps_disclosure_with_stable_source_id():
    payload=[{"publishDate":"21.08.2026 12:30:00","summary":"Yeni sözleşme","kapTitle":"Türk Hava Yolları",
              "stockCodes":"THYAO","relatedStocks":None,"disclosureIndex":12345}]
    provider=RealKapProvider(opener=lambda request,timeout:io.BytesIO(json.dumps(payload).encode()),
                            now=lambda:datetime(2026,8,21,10,tzinfo=timezone.utc))
    events=provider.fetch(["THYAO.IS"])
    assert len(events)==1 and events[0].id=="kap:12345:THYAO" and events[0].symbol=="THYAO.IS"
    assert events[0].url.endswith("/12345") and provider.availability=="AVAILABLE"


def test_real_news_provider_maps_only_actual_response_items():
    payload={"news":[{"uuid":"news-1","title":"THYAO kapasite açıklaması","publisher":"Example Finance",
                      "relatedTickers":["THYAO.IS"],
                      "providerPublishTime":int(datetime(2026,8,21,9,tzinfo=timezone.utc).timestamp()),
                      "link":"https://example.test/news-1","summary":"Açıklanan kapasite bilgisi"}]}
    provider=YahooFinanceNewsProvider(opener=lambda request,timeout:io.BytesIO(json.dumps(payload).encode()),
                                      now=lambda:datetime(2026,8,21,10,tzinfo=timezone.utc))
    events=provider.fetch(["THYAO.IS"])
    assert len(events)==1 and events[0].id=="yahoo:news-1:THYAO.IS"
    assert events[0].title=="THYAO kapasite açıklaması" and provider.availability=="AVAILABLE"


def test_yahoo_transport_normalizes_real_chart_response():
    timestamp=int(datetime(2026,8,21,12,0,tzinfo=timezone.utc).timestamp())
    payload={"chart":{"result":[{"timestamp":[timestamp],"indicators":{"quote":[{
        "open":[300],"high":[305],"low":[299],"close":[301],"volume":[123456]}]}}]}}
    def opener(request,timeout): return io.BytesIO(json.dumps(payload).encode())
    candles=YahooChartTransport(opener=opener,sleeper=lambda _:None)._one("THYAO.IS",60,"1h",5)
    assert candles[0].symbol=="THYAO.IS" and candles[0].close==301 and candles[0].volume==123456


def test_current_session_close_is_not_stale_after_market_closes():
    now=datetime(2026,8,21,15,30,tzinfo=timezone.utc)  # 18:30 Istanbul
    closing_bar=datetime(2026,8,21,14,45,tzinfo=timezone.utc)
    assert not _is_stale_for_bist_session(now,closing_bar,timedelta(minutes=30))
    assert _is_stale_for_bist_session(now,closing_bar-timedelta(days=1),timedelta(minutes=30))


def test_no_news_and_unavailable_llm_are_neutral_not_bearish():
    analysis=hold_analysis("THYAO.IS","not required",status=LLMStatus.NOT_REQUIRED)
    assert analysis.priced_in_probability==50 and analysis.risk_score==50 and analysis.confidence==0
    ranking=RankedEventCandidate(symbol="THYAO.IS",scanner_score=90,event_score=50,combined_score=90,
                                 intelligence_status=IntelligenceStatus.NO_NEWS)
    decision=DeterministicStrategyEngine(load_settings("config.v1.yaml").scoring).evaluate(technical(),ranking,analysis)
    assert decision.action is Action.BUY and decision.signal_mode.value=="TECHNICAL_ONLY"
