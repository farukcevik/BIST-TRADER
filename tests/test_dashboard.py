from __future__ import annotations

import inspect
import json
import sqlite3
from datetime import datetime,timezone

import pytest

import dashboard
from bistbot.app.config import load_settings
from bistbot.app.models import Action,RiskDecision,RiskOutcome,RiskReasonCode,TradeSignal
from bistbot.broker.paper import PaperBroker
from bistbot.dashboard_data import DashboardDataService,parse_dashboard_timestamps
from bistbot.storage.database import Database

NOW=datetime(2026,8,24,12,tzinfo=timezone.utc)


def test_dashboard_projects_paper_state_without_write_access(tmp_path):
    path=tmp_path/"dashboard.db"; settings=load_settings("config.v1.yaml"); database=Database(str(path))
    broker=PaperBroker(10_000,database=database,risk_settings=settings.risk,clock=lambda:NOW)
    signal=TradeSignal(symbol="GUBRF.IS",action=Action.BUY,score=75,reason="dashboard fixture",
        strategy_version="test",requested_price=100,timestamp=NOW)
    decision=RiskDecision(signal_id=signal.id,outcome=RiskOutcome.APPROVE,
        reason_code=RiskReasonCode.APPROVED,reason="approved",approved_quantity=10)
    broker.buy(signal,decision)
    database.execute("INSERT INTO system_events(timestamp,level,event_type,message,payload) VALUES(?,?,?,?,?)",
        (NOW.isoformat(),"INFO","CYCLE_COMPLETED","done",json.dumps({"summary":{"scanner_candidates":40,
        "news_provider_status":"AVAILABLE_NO_EVENTS"},"decisions":[{"symbol":"GUBRF.IS","decision":"BUY"}]})))
    database.close()

    service=DashboardDataService(path,settings); data=service.load()
    assert data["mode"]=="PAPER" and data["summary"]["open_positions"]==1
    assert data["positions"][0]["symbol"]=="GUBRF.IS" and data["trades"][0]["side"]=="BUY"
    assert data["cycle"]["summary"]["scanner_candidates"]==40
    assert data["decisions"][0]["decision"]=="BUY"
    with service.connection() as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE metadata SET value='0' WHERE key='paper_cash'")


def test_dashboard_does_not_fabricate_history(tmp_path):
    path=tmp_path/"empty.db"; settings=load_settings("config.v1.yaml")
    Database(str(path)).close()
    data=DashboardDataService(path,settings).load()
    assert data["history"]==[] and data["positions"]==[] and data["trades"]==[]


def test_dashboard_uses_persisted_position_stop_with_legacy_fallback(tmp_path):
    path=tmp_path/"stops.db"; settings=load_settings("config.v1.yaml"); database=Database(str(path))
    broker=PaperBroker(10_000,database=database,risk_settings=settings.risk,clock=lambda:NOW)
    for symbol in ("ACTIVE.IS","LEGACY.IS"):
        signal=TradeSignal(symbol=symbol,action=Action.BUY,score=75,reason="fixture",
            strategy_version="test",requested_price=100,timestamp=NOW)
        broker.buy(signal,RiskDecision(signal_id=signal.id,outcome=RiskOutcome.APPROVE,
            reason_code=RiskReasonCode.APPROVED,reason="approved",approved_quantity=10))
    database.execute("UPDATE paper_positions SET stop_price=? WHERE symbol=?",("96.25","ACTIVE.IS"))
    database.close()

    positions={item["symbol"]:item for item in DashboardDataService(path,settings).load()["positions"]}

    assert positions["ACTIVE.IS"]["stop_price"]==96.25
    assert positions["LEGACY.IS"]["stop_price"]==pytest.approx(100*(1-settings.risk.default_stop_loss_pct))


def test_dashboard_projects_investment_analysis_from_cycle_payload(tmp_path):
    path=tmp_path/"analysis.db"; settings=load_settings("config.v1.yaml"); database=Database(str(path))
    decision={"symbol":"AAA.IS","decision":"HOLD","reason_code":"INSUFFICIENT_RISK_REWARD",
        "reason":"risk/reward below minimum","fundamental_provider_status":"PARTIAL",
        "fundamental":{"fundamental_score":71,"confidence":64,"growth_score":68,"red_flags":["NET_DEBT_RISING"]},
        "technical_levels":{"support_1":98,"resistance_1":106,"market_structure":"RANGE"},
        "potential":{"entry_price":102,"downside_reference":98,"expected_target_price":106,
            "expected_upside_pct":3.92,"downside_risk_pct":3.92,"risk_reward_ratio":1,"potential_score":44}}
    database.execute("INSERT INTO system_events(timestamp,level,event_type,message,payload) VALUES(?,?,?,?,?)",
        (NOW.isoformat(),"INFO","CYCLE_COMPLETED","done",json.dumps({"summary":{},"decisions":[decision]})))
    database.close()

    projected=DashboardDataService(path,settings).load()["decisions"][0]
    view=dashboard.investment_analysis(projected)

    assert projected["fundamental"]["provider_status"]=="PARTIAL"
    assert view["fundamental"]["Fundamental score"]==71
    assert view["levels"]["Support 1"]==98
    assert view["potential"]["Risk / reward"]==1
    assert view["decision"]["Reason code"]=="INSUFFICIENT_RISK_REWARD"


def test_dashboard_enriches_decision_from_latest_optional_snapshots(tmp_path):
    path=tmp_path/"snapshots.db"; settings=load_settings("config.v1.yaml"); database=Database(str(path))
    database.execute("INSERT INTO fundamental_scores(timestamp,symbol,score,confidence,provider_status,payload) VALUES(?,?,?,?,?,?)",
        ("2026-08-23T12:00:00+00:00","AAA.IS",10,20,"PARTIAL",json.dumps({"fundamental_score":10})))
    database.execute("INSERT INTO fundamental_scores(timestamp,symbol,score,confidence,provider_status,payload) VALUES(?,?,?,?,?,?)",
        (NOW.isoformat(),"AAA.IS",82,90,"AVAILABLE",json.dumps({"fundamental_score":82,"provider_status":"AVAILABLE"})))
    database.execute("INSERT INTO technical_levels(timestamp,symbol,confidence,payload) VALUES(?,?,?,?)",
        (NOW.isoformat(),"AAA.IS",80,json.dumps({"support_1":99,"resistance_1":112})))
    database.execute("INSERT INTO potential_assessments(timestamp,symbol,score,confidence,payload) VALUES(?,?,?,?,?)",
        (NOW.isoformat(),"AAA.IS",72,75,json.dumps({"expected_target_price":112,"risk_reward_ratio":2.2})))
    database.close()

    decision=DashboardDataService(path,settings).load()["decisions"][0]

    assert decision["fundamental"]["fundamental_score"]==82
    assert decision["technical_levels"]["support_1"]==99
    assert decision["potential"]["risk_reward_ratio"]==2.2


def test_dashboard_joins_kap_snapshot_audit_without_fabricating_missing_fields(tmp_path):
    path=tmp_path/"kap-audit.db"; settings=load_settings("config.v1.yaml"); database=Database(str(path))
    database.execute("INSERT INTO fundamental_snapshots(timestamp,symbol,provider_status,payload) VALUES(?,?,?,?)",
        (NOW.isoformat(),"AAA.IS","PARTIAL",json.dumps({"source":"KAP","as_of":NOW.isoformat(),
            "audit_metadata":{"official_source":"https://www.kap.org.tr"},
            "periods":[{"period_end":"2026-06-30T00:00:00+00:00","currency":"TRY",
                "unit":1000,"consolidated":True}]})))
    database.execute("INSERT INTO fundamental_scores(timestamp,symbol,score,confidence,provider_status,payload) VALUES(?,?,?,?,?,?)",
        (NOW.isoformat(),"AAA.IS",71,64,"PARTIAL",json.dumps({"fundamental_score":71})))
    database.close()

    decision=DashboardDataService(path,settings).load()["decisions"][0]
    view=dashboard.investment_analysis(decision)

    assert view["fundamental"]["Provider status"]=="PARTIAL"
    assert view["fundamental_audit"]=={"Source":"KAP","Statement as of":NOW.isoformat(),
        "Latest period":"2026-06-30T00:00:00+00:00","Currency":"TRY","Unit":1000,
        "Statement scope":"CONSOLIDATED"}
    missing=dashboard.investment_analysis({"fundamental":{"provider_status":"UNAVAILABLE"}})
    assert all(value is None for value in missing["fundamental_audit"].values())


def test_investment_analysis_preserves_zero_scores_and_marks_missing_values():
    view=dashboard.investment_analysis({"symbol":"AAA.IS","fundamental":{"fundamental_score":0}})
    assert view["fundamental"]["Fundamental score"]==0
    assert dashboard.display_value(view["potential"]["Target"])=="Unavailable"


def test_investment_analysis_formats_persisted_units_consistently():
    assert dashboard.format_analysis_value("levels","Support 1",98.125)=="₺98.12"
    assert dashboard.format_analysis_value("potential","Expected upside",.75)=="+0.75%"
    assert dashboard.format_analysis_value("potential","Downside risk",3.925)=="+3.92%"
    assert dashboard.format_analysis_value("potential","Risk / reward",2.2)=="2.20x"
    assert dashboard.format_analysis_value("potential","Potential score",72)=="72.00/100"
    assert dashboard.format_analysis_value("potential","Confidence",64)=="64.00%"


@pytest.mark.parametrize("entry",[None,0,-1,float("nan"),"102"])
def test_investment_analysis_hides_entry_dependent_levels_when_entry_invalid(entry):
    view=dashboard.investment_analysis({"potential":{"entry_price":entry,"downside_reference":98,
        "expected_target_price":106,"risk_reward_ratio":2}})
    assert view["potential"]["Entry"] is None
    assert view["potential"]["Stop"] is None
    assert view["potential"]["Target"] is None
    assert view["potential"]["Risk / reward"] is None


def test_dashboard_live_positions_uses_configured_yahoo_quote_freshness(monkeypatch):
    calls={}

    class FakeProvider:
        def __init__(self,*,yahoo_execution_freshness_seconds):
            calls["freshness"]=yahoo_execution_freshness_seconds

    def fake_refresh(positions,provider,*,refresh_seconds):
        calls.update(positions=positions,provider=provider,refresh_seconds=refresh_seconds)
        return []

    monkeypatch.setattr(dashboard,"YahooBistProvider",FakeProvider)
    monkeypatch.setattr(dashboard,"refresh_live_positions",fake_refresh)

    assert dashboard.cached_live_positions.__wrapped__(((("symbol","AAA.IS"),),))==[]
    assert calls["freshness"]==dashboard.SETTINGS.market_data.yahoo_execution_freshness_seconds
    assert calls["positions"]==[{"symbol":"AAA.IS"}]
    assert calls["refresh_seconds"]==dashboard.REFRESH_SECONDS


def test_dashboard_projects_configured_override_effective_and_control_versions_read_only(tmp_path):
    path=tmp_path/"controls.db"; settings=load_settings("config.v1.yaml"); database=Database(str(path))
    database.execute("INSERT INTO runtime_settings(key,value,updated_at,version) VALUES(?,?,?,?)",
        ("paper.max_open_positions","3",NOW.isoformat(),2))
    database.execute("INSERT INTO runtime_settings(key,value,updated_at,version) VALUES(?,?,?,?)",
        ("paper.cash.version","4",NOW.isoformat(),1))
    database.close()

    risk=DashboardDataService(path,settings).load()["risk"]

    assert risk["configured_max_open_positions"]==settings.risk.max_open_positions
    assert risk["max_open_positions_override"]==risk["max_open_positions"]==3
    assert risk["max_open_positions_version"]==2 and risk["cash_version"]==4


def test_dashboard_exposes_no_portfolio_mutation_controls_or_services():
    source=inspect.getsource(dashboard)
    assert not hasattr(dashboard,"apply_paper_control")
    assert not hasattr(dashboard,"paper_portfolio_controls")
    for forbidden in ("PaperPortfolioControlService","form_submit_button","set_cash(","set_max_open_positions("):
        assert forbidden not in source


def test_mixed_iso_timestamps_and_malformed_value_are_tolerated():
    values=["2026-08-24T18:45:38+00:00","2026-08-24T18:45:38.925740+00:00",
        "2026-08-24T18:45:38Z","not-a-time"]
    parsed=parse_dashboard_timestamps(values)
    assert parsed.notna().tolist()==[True,True,True,False]
    assert str(parsed.dt.tz)=="UTC"


def test_closed_trade_metrics_use_persisted_fills_and_activity_is_newest_first(tmp_path):
    path=tmp_path/"closed.db"; settings=load_settings("config.v1.yaml"); database=Database(str(path))
    broker=PaperBroker(10_000,commission_pct=.001,database=database,risk_settings=settings.risk,clock=lambda:NOW)
    buy=TradeSignal(symbol="AAA.IS",action=Action.BUY,score=72,reason="ENTRY",strategy_version="test",
        requested_price=100,timestamp=NOW)
    buy_decision=RiskDecision(signal_id=buy.id,outcome=RiskOutcome.APPROVE,
        reason_code=RiskReasonCode.APPROVED,reason="approved",approved_quantity=10)
    broker.buy(buy,buy_decision)
    sell=TradeSignal(symbol="AAA.IS",action=Action.SELL,score=20,reason="TRAILING_STOP",strategy_version="test",
        requested_price=110,timestamp=NOW.replace(hour=14))
    sell_decision=RiskDecision(signal_id=sell.id,outcome=RiskOutcome.APPROVE,
        reason_code=RiskReasonCode.APPROVED,reason="approved",approved_quantity=10)
    broker.sell(sell,10,sell_decision); database.close()

    data=DashboardDataService(path,settings).load(); closed=data["closed_positions"][0]
    assert [item["side"] for item in data["trades"]]==["SELL","BUY"]
    assert closed["realized_pnl"]==pytest.approx(98.9)
    assert closed["commission_costs"]==pytest.approx(2.1)
    assert closed["exit_reason"]=="TRAILING_STOP" and closed["entry_score"]==72
    assert data["summary"]["winning_closed_trades"]==1
    assert data["summary"]["losing_closed_trades"]==0 and data["summary"]["win_rate"]==100


def test_total_net_pnl_is_equity_minus_capital_not_realized_plus_unrealized(tmp_path):
    path=tmp_path/"pnl.db"; settings=load_settings("config.v1.yaml"); database=Database(str(path))
    broker=PaperBroker(10_000,database=database,risk_settings=settings.risk,clock=lambda:NOW)
    signal=TradeSignal(symbol="AAA.IS",action=Action.BUY,score=75,reason="ENTRY",strategy_version="test",
        requested_price=100,timestamp=NOW)
    decision=RiskDecision(signal_id=signal.id,outcome=RiskOutcome.APPROVE,
        reason_code=RiskReasonCode.APPROVED,reason="approved",approved_quantity=10)
    broker.buy(signal,decision); broker.refresh_position("AAA.IS",110,data_timestamp=NOW); database.close()
    data=DashboardDataService(path,settings).load()
    assert data["summary"]["equity"]-data["summary"]["initial_capital"]==pytest.approx(100)
    assert data["summary"]["realized"]==0 and data["summary"]["unrealized"]==pytest.approx(100)
