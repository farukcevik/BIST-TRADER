from __future__ import annotations

import io,json
from datetime import datetime,timezone

from bistbot.app.config import load_settings
from bistbot.app.models import (Action,IntelligenceStatus,LLMAnalysisInput,LLMStatus,RankedEventCandidate,
                                TechnicalSignal)
from bistbot.intelligence.llm_provider import hold_analysis
from bistbot.market.provider import BistSymbolUniverse,YahooChartTransport
from bistbot.strategy.engine import DeterministicStrategyEngine


def technical(symbol="THYAO.IS",score=90):
    return TechnicalSignal(symbol=symbol,timestamp=datetime.now(timezone.utc),technical_score=score,
        momentum_score=score,volume_score=score,trend_score=score,liquidity_score=75,volatility_score=60,
        overall_scanner_score=score,metrics={},reasons=[])


def test_maintained_universe_contains_real_tickers_only():
    universe=BistSymbolUniverse(); symbols=universe.load()
    assert len(symbols)>=100 and not universe.invalid_symbols
    assert {"THYAO.IS","ASELS.IS","TUPRS.IS","EREGL.IS","CWENE.IS","MAGEN.IS"}<=set(symbols)
    assert not any(symbol.startswith("BIST") and symbol[4:7].isdigit() for symbol in symbols)


def test_yahoo_transport_normalizes_real_chart_response():
    timestamp=int(datetime(2026,8,21,12,0,tzinfo=timezone.utc).timestamp())
    payload={"chart":{"result":[{"timestamp":[timestamp],"indicators":{"quote":[{
        "open":[300],"high":[305],"low":[299],"close":[301],"volume":[123456]}]}}]}}
    def opener(request,timeout): return io.BytesIO(json.dumps(payload).encode())
    candles=YahooChartTransport(opener=opener,sleeper=lambda _:None)._one("THYAO.IS",60,"1h",5)
    assert candles[0].symbol=="THYAO.IS" and candles[0].close==301 and candles[0].volume==123456


def test_no_news_and_unavailable_llm_are_neutral_not_bearish():
    analysis=hold_analysis("THYAO.IS","not required",status=LLMStatus.NOT_REQUIRED)
    assert analysis.priced_in_probability==50 and analysis.risk_score==50 and analysis.confidence==0
    ranking=RankedEventCandidate(symbol="THYAO.IS",scanner_score=90,event_score=50,combined_score=90,
                                 intelligence_status=IntelligenceStatus.NO_NEWS)
    decision=DeterministicStrategyEngine(load_settings("config.v1.yaml").scoring).evaluate(technical(),ranking,analysis)
    assert decision.action is Action.BUY and decision.signal_mode.value=="TECHNICAL_ONLY"
