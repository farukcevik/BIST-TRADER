from __future__ import annotations

from datetime import datetime,timezone
import io,json,sqlite3,sys
from types import SimpleNamespace

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


@pytest.mark.parametrize(
    ("mode_args","expected_opened","expected_application","expected_broker"),
    [
        (["--analyze-symbol","THYAO.IS"],["configured",":memory:"],"configured",":memory:"),
        (["--diagnose-intelligence"],[":memory:"],":memory:",":memory:"),
        (["--diagnose-regime"],[":memory:"],":memory:",":memory:"),
    ],
)
def test_cli_database_selection_keeps_symbol_analysis_on_paper_cache_without_execution(
    tmp_path,monkeypatch,capsys,mode_args,expected_opened,expected_application,expected_broker
):
    paper_path=tmp_path/"paper.db"
    raw=yaml.safe_load(open("config.v1.yaml",encoding="utf-8")); raw["database"]=str(paper_path)
    config_path=tmp_path/"config.yaml"; config_path.write_text(yaml.safe_dump(raw),encoding="utf-8")
    opened=[]; closed=[]; application_databases=[]; broker_databases=[]; broker_calls=[]; queries=[]

    class FakeDatabase:
        def __init__(self,path,**kwargs):
            self.path=str(path); opened.append(self.path)
        def query(self,sql,*args):
            queries.append((self.path,sql))
            return [{"symbol":"THYAO.IS"}] if "paper_positions" in sql else []
        def close(self): closed.append(self.path)

    class FakeBroker:
        def __init__(self,*args,database,**kwargs):
            assert database.path in {str(paper_path),":memory:"}
            broker_databases.append(database.path)
            self.calendar=SimpleNamespace(status=lambda:SimpleNamespace(
                local_time=NOW,is_trading_day=True,
                current_session=SimpleNamespace(value="CONTINUOUS"),
                can_execute_orders=True,reason="test"))
        def buy(self,*args,**kwargs): broker_calls.append("buy")
        def sell(self,*args,**kwargs): broker_calls.append("sell")

    class FakeApplication:
        def __init__(self,settings,database,*args,**kwargs):
            application_databases.append(database.path); self.diagnostic_existing_positions=set()
        def provider_status(self): return []
        def run_cycle(self,**kwargs): raise AssertionError("trading cycle must not run")
        def analyze_symbol(self,symbol):
            assert symbol=="THYAO.IS"
            assert self.diagnostic_existing_positions=={"THYAO.IS"}
            return {"symbol":symbol,"risk":{"status":"SIMULATED / NO EXECUTION"}}
        def diagnose_intelligence(self):
            base={"provider":"Fake","status":"AVAILABLE_NO_EVENTS"}
            return {"news":base,"kap":base,"openai":{"provider":"Disabled","status":"UNAVAILABLE"}}
        def diagnose_regime(self):
            return {"provider":"Fake","provider_status":"AVAILABLE","regime":{
                "regime":"NEUTRAL","market_risk_score":0,"confidence":0,"risk_categories":{}},"events":[]}

    monkeypatch.setattr(cli,"Database",FakeDatabase)
    monkeypatch.setattr(cli,"PaperBroker",FakeBroker)
    monkeypatch.setattr(cli,"BistBotApplication",FakeApplication)
    monkeypatch.setattr(sys,"argv",["main.py","--config",str(config_path),*mode_args])

    assert cli.main()==0
    resolve=lambda value:str(paper_path) if value=="configured" else value
    assert opened==[resolve(value) for value in expected_opened]
    assert application_databases==[resolve(expected_application)]
    assert broker_databases==[resolve(expected_broker)]
    assert broker_calls==[]
    expected_closed=list(reversed(opened)) if mode_args[0]=="--analyze-symbol" else opened
    assert closed==expected_closed
    if mode_args[0]=="--analyze-symbol":
        assert queries==[(str(paper_path),"SELECT symbol FROM paper_positions")]
    output=capsys.readouterr().out
    expected_output=("SIMULATED / NO EXECUTION" if mode_args[0]!="--diagnose-intelligence"
                     else "INTELLIGENCE DIAGNOSTICS")
    assert expected_output in output


def test_symbol_analysis_does_not_initialize_accounting_in_real_paper_database(tmp_path,monkeypatch):
    paper_path=tmp_path/"paper.db"
    raw=yaml.safe_load(open("config.v1.yaml",encoding="utf-8")); raw["database"]=str(paper_path)
    config_path=tmp_path/"config.yaml"; config_path.write_text(yaml.safe_dump(raw),encoding="utf-8")

    class FakeApplication:
        def __init__(self,settings,database,broker,*args,**kwargs):
            assert database.path==str(paper_path)
            assert broker.database.path==":memory:"
            self.diagnostic_existing_positions=set()
        def provider_status(self): return []
        def run_cycle(self,**kwargs): raise AssertionError("trading cycle must not run")
        def analyze_symbol(self,symbol):
            return {"symbol":symbol,"risk":{"status":"SIMULATED / NO EXECUTION"}}

    monkeypatch.setattr(cli,"BistBotApplication",FakeApplication)
    monkeypatch.setattr(cli,"load_project_environment",lambda:False)
    monkeypatch.setattr(sys,"argv",["main.py","--config",str(config_path),"--analyze-symbol","THYAO.IS"])

    assert cli.main()==0
    connection=sqlite3.connect(paper_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM metadata").fetchone()[0]==0
        assert connection.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]==0
        assert connection.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]==0
    finally:
        connection.close()


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
