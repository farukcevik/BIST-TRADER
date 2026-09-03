from __future__ import annotations

import json
import sqlite3
from datetime import datetime,timezone

import pytest

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
