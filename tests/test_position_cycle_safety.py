from __future__ import annotations

from datetime import datetime,timedelta,timezone
from decimal import Decimal

from bistbot.app.config import load_settings
from bistbot.app.models import (Action,Candle,MarketDataResult,MarketSnapshot,ProviderDiagnostics,
                                RiskDecision,RiskOutcome,RiskReasonCode,TradeSignal)
from bistbot.app.runtime import BistBotApplication
from bistbot.broker.paper import PaperBroker
from bistbot.intelligence.kap_provider import MockKapProvider
from bistbot.intelligence.news_provider import MockNewsProvider
from bistbot.main import paper_status
from bistbot.notifications.base import SafeNotificationDispatcher
from bistbot.storage.database import Database

NOW=datetime(2026,8,24,12,tzinfo=timezone.utc)


def approved(signal,quantity=10):
    return RiskDecision(signal_id=signal.id,outcome=RiskOutcome.APPROVE,
        reason_code=RiskReasonCode.APPROVED,reason="approved",approved_quantity=quantity)


def open_position(broker,symbol="AAA.IS",price=100):
    broker.clock=lambda:NOW-timedelta(days=3)
    signal=TradeSignal(symbol=symbol,action=Action.BUY,score=75,reason="fixture",
        strategy_version="test",requested_price=price,timestamp=NOW-timedelta(days=3))
    broker.buy(signal,approved(signal)); return signal


def result(symbol,price,*,available=True,timestamp=NOW):
    diagnostics=ProviderDiagnostics(symbol=symbol,request_timestamp=NOW,data_timestamp=timestamp if available else None,
        latency_ms=1,is_stale=not available,error_reason=None if available else "stale fixture",attempts=1)
    if not available: return MarketDataResult(symbol=symbol,diagnostics=diagnostics)
    candle=Candle(symbol=symbol,timestamp=timestamp,open=price,high=price,low=price,close=price,volume=100000)
    return MarketDataResult(symbol=symbol,candles=[candle],snapshot=MarketSnapshot(
        symbol=symbol,timestamp=timestamp,price=price,volume=100000),diagnostics=diagnostics)


class PositionFirstMarket:
    provider_mode="MOCK"
    def __init__(self,position_result): self.position_result=position_result; self.calls=[]
    def intraday(self,symbols,**kwargs):
        self.calls.append(tuple(symbols))
        return {symbol:self.position_result for symbol in symbols} if symbols else {}
    def active_symbols(self): self.calls.append(("UNIVERSE",)); return []


def application(tmp_path,position_result):
    settings=load_settings("config.v1.yaml"); database=Database(str(tmp_path/"cycle.db"))
    broker=PaperBroker(10_000,database=database,risk_settings=settings.risk)
    open_position(broker)
    market=PositionFirstMarket(position_result)
    app=BistBotApplication(settings,database,broker,SafeNotificationDispatcher(),market=market,
        news=MockNewsProvider(),kap=MockKapProvider())
    return app,broker,database,market


def test_old_position_is_refreshed_before_scanner(tmp_path):
    app,broker,database,market=application(tmp_path,result("AAA.IS",105))
    old=broker.get_positions()["AAA.IS"].updated_at
    original_scan=app.scanner.scan
    def assert_refreshed(*args,**kwargs):
        row=database.query("SELECT * FROM paper_positions WHERE symbol='AAA.IS'")[0]
        assert row["last_price"]=="105.0" and row["position_status"]=="FRESH"
        assert datetime.fromisoformat(row["updated_at"])>old
        return original_scan(*args,**kwargs)
    app.scanner.scan=assert_refreshed
    app.run_cycle(now=NOW)
    assert market.calls[0]==("AAA.IS",) and market.calls[1]==("UNIVERSE",)


def test_stop_exit_and_cash_recalculation_precede_scanner(tmp_path):
    app,broker,database,_=application(tmp_path,result("AAA.IS",90))
    cash_before=broker.get_cash(); original_scan=app.scanner.scan
    def assert_exit_completed(*args,**kwargs):
        assert broker.get_positions()=={}
        assert broker.get_cash()>cash_before
        return original_scan(*args,**kwargs)
    app.scanner.scan=assert_exit_completed
    summary=app.run_cycle(now=NOW)
    assert summary["exit_orders"]==1 and summary["entry_orders"]==0
    assert app.last_diagnostics["post_exit_portfolio"]["open_positions"]==0
    assert database.query("SELECT COUNT(*) n FROM system_events WHERE event_type='CYCLE_COMPLETED'")[0]["n"]==1


def test_stale_position_is_flagged_and_old_price_not_used_for_exit(tmp_path):
    app,broker,database,_=application(tmp_path,result("AAA.IS",0,available=False))
    summary=app.run_cycle(now=NOW)
    row=database.query("SELECT * FROM paper_positions WHERE symbol='AAA.IS'")[0]
    assert row["position_status"]=="STALE_MARKET_DATA" and row["last_price"]=="100.0000"
    assert summary["exit_orders"]==0
    assert app.last_diagnostics["positions"][0]["action"]=="STALE_MARKET_DATA — NO ASSUMPTION"


def test_high_water_and_trailing_stop_never_move_down(tmp_path):
    settings=load_settings("config.v1.yaml"); database=Database(str(tmp_path/"high.db"))
    broker=PaperBroker(10_000,database=database,risk_settings=settings.risk); open_position(broker)
    high=broker.refresh_position("AAA.IS",Decimal("110"),data_timestamp=NOW,current_score=70)
    lower=broker.refresh_position("AAA.IS",Decimal("104"),data_timestamp=NOW+timedelta(minutes=1),current_score=65)
    assert high==lower==Decimal("110")
    trailing_before=high*(Decimal("1")-Decimal(str(settings.risk.default_trailing_stop_pct)))
    trailing_after=lower*(Decimal("1")-Decimal(str(settings.risk.default_trailing_stop_pct)))
    assert trailing_after>=trailing_before


def test_status_exposes_stale_data_and_percentage_points(tmp_path):
    settings=load_settings("config.v1.yaml"); database=Database(str(tmp_path/"status.db"))
    broker=PaperBroker(10_000,database=database,risk_settings=settings.risk); open_position(broker)
    broker.mark_position_stale("AAA.IS")
    status=paper_status(database,settings); item=status["positions"]["AAA.IS"]
    assert item["market_data_status"]=="STALE_MARKET_DATA" and status["warnings"]
    assert item["unrealized_pnl_pct"].endswith("%")
