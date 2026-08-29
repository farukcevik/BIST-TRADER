from __future__ import annotations

from datetime import datetime,timezone
import io,json,sys

import pytest
import yaml

import bistbot.main as cli
from bistbot.intelligence.kap_provider import RealKapProvider
from bistbot.intelligence.news_provider import YahooFinanceNewsProvider


NOW=datetime(2026,8,21,10,tzinfo=timezone.utc)


def response(payload):
    return io.BytesIO(json.dumps(payload,ensure_ascii=False).encode())


def test_news_success_empty_and_diagnostics():
    provider=YahooFinanceNewsProvider(opener=lambda *a,**k:response({"news":[]}),now=lambda:NOW)
    assert provider.fetch(["CWENE.IS"])==[]
    diag=provider.last_diagnostics
    assert diag.status.value=="AVAILABLE_NO_EVENTS" and diag.http_status==200
    assert diag.raw_events==diag.parsed_events==diag.symbols_matched==0


def test_news_network_failure_is_unavailable():
    provider=YahooFinanceNewsProvider(opener=lambda *a,**k:(_ for _ in ()).throw(OSError("offline")),
                                      now=lambda:NOW,sleeper=lambda _:None)
    with pytest.raises(OSError): provider.fetch(["CWENE.IS"])
    assert provider.last_diagnostics.status.value=="UNAVAILABLE"
    assert provider.last_diagnostics.error_type=="OSError"


def test_news_deduplicates_same_source_id():
    item={"uuid":"same","title":"CWENE duyuru","publisher":"X","relatedTickers":["CWENE.IS"],
          "providerPublishTime":int(NOW.timestamp()),"link":"https://example.test/x"}
    provider=YahooFinanceNewsProvider(opener=lambda *a,**k:response({"news":[item,item]}),now=lambda:NOW)
    assert len(provider.fetch(["CWENE.IS"]))==1
    assert provider.last_diagnostics.status.value=="AVAILABLE_WITH_EVENTS"


def test_kap_success_empty_and_parsing_failure():
    empty=RealKapProvider(opener=lambda *a,**k:response([]),now=lambda:NOW)
    assert empty.fetch(["CWENE.IS"])==[]
    assert empty.last_diagnostics.status.value=="AVAILABLE_NO_EVENTS"
    broken=RealKapProvider(opener=lambda *a,**k:response({"unexpected":True}),now=lambda:NOW,sleeper=lambda _:None)
    with pytest.raises(ValueError): broken.fetch(["CWENE.IS"])
    assert broken.last_diagnostics.status.value=="ERROR"


def test_diagnostic_cli_modes_never_run_cycle(tmp_path,monkeypatch,capsys):
    raw=yaml.safe_load(open("config.v1.yaml",encoding="utf-8")); raw["database"]=str(tmp_path/"cli.db")
    path=tmp_path/"config.yaml"; path.write_text(yaml.safe_dump(raw),encoding="utf-8")
    class FakeApplication:
        def __init__(self,*a,**k): pass
        def provider_status(self): return []
        def run_cycle(self,**kwargs): raise AssertionError("trading cycle must not run")
        def diagnose_intelligence(self):
            base={"provider":"Fake","mode":"MOCK","status":"AVAILABLE_NO_EVENTS"}
            return {"news":base,"kap":base,"openai":{"provider":"Disabled","status":"UNAVAILABLE","model":None}}
        def analyze_symbol(self,symbol):
            return {"symbol":symbol,"market_data":{},"news":{},"kap":{},"llm":{"invoked":"NO"},
                    "strategy":{"decision":"HOLD"},"risk":{"status":"SIMULATED / NO EXECUTION"}}
    monkeypatch.setattr(cli,"BistBotApplication",FakeApplication)
    monkeypatch.setattr(sys,"argv",["main.py","--config",str(path),"--diagnose-intelligence"])
    assert cli.main()==0 and "INTELLIGENCE DIAGNOSTICS" in capsys.readouterr().out
    monkeypatch.setattr(sys,"argv",["main.py","--config",str(path),"--analyze-symbol","CWENE.IS"])
    assert cli.main()==0 and "SIMULATED / NO EXECUTION" in capsys.readouterr().out


def test_symbol_action_summary_is_concise_and_explains_existing_position(capsys):
    result={"symbol":"GUBRF.IS","market_data":{},"news":{},"kap":{},"llm":{},"strategy":{},"risk":{},
        "action_summary":{"symbol":"GUBRF.IS","latest_price":475.75,
            "technical":{"scanner_score":74.9131,"strength":"STRONG"},
            "volume":{"relative_volume":2.537,"strength":"STRONG"},
            "kap":{"found":4,"low_materiality_ignored":4,"material_events_analyzed":0},
            "llm":{"invoked":"NO","status":"NOT_REQUIRED","bias":"HOLD","confidence":0,"catalyst":0},
            "score":{"technical_contribution":72.509,"news_contribution":0,"llm_contribution":0,
                "final":72.509,"buy_threshold":72},
            "decision":"MANAGE_EXISTING_POSITION","strategy_decision":"BUY",
            "reason":"BUY setup passed, but position exists; pyramiding is disabled.",
            "execution":"SIMULATED / NO EXECUTION"}}
    cli.print_symbol_analysis(result); output=capsys.readouterr().out
    assert "ACTION SUMMARY" in output and "VOLUME             2.54x  STRONG" in output
    assert "4 low-materiality ignored" in output
    assert "DECISION                  MANAGE_EXISTING_POSITION" in output
