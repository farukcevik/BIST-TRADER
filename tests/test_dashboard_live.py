from __future__ import annotations

from datetime import datetime,timedelta,timezone
import pytest

from bistbot.app.models import MarketDataResult,MarketSnapshot,ProviderDiagnostics
from bistbot.dashboard_live import live_portfolio_summary,refresh_live_positions

NOW=datetime(2026,8,24,8,0,tzinfo=timezone.utc)


def market_result(symbol,price,timestamp=NOW,available=True):
    diagnostics=ProviderDiagnostics(symbol=symbol,request_timestamp=NOW,
        data_timestamp=timestamp if available else None,latency_ms=1,is_stale=not available,
        error_reason=None if available else "quote unavailable",attempts=1)
    return MarketDataResult(symbol=symbol,snapshot=MarketSnapshot(symbol=symbol,timestamp=timestamp,
        price=price,volume=1000) if available else None,diagnostics=diagnostics)


class QuoteSpy:
    def __init__(self,results): self.results=results; self.calls=[]
    def snapshots(self,symbols): self.calls.append(list(symbols)); return self.results
    def complete(self,*args,**kwargs): raise AssertionError("dashboard called LLM")
    def fetch(self,*args,**kwargs): raise AssertionError("dashboard called news/KAP")
    def buy(self,*args,**kwargs): raise AssertionError("dashboard executed an order")
    def sell(self,*args,**kwargs): raise AssertionError("dashboard executed an order")


def positions():
    return [{"symbol":"AAA.IS","quantity":10,"average_entry":100.0,"last_price":98.0,
        "stop_price":95.0,"take_profit_price":110.0,"trailing_stop":97.0,"highest_price":105.0},
        {"symbol":"BBB.IS","quantity":5,"average_entry":200.0,"last_price":190.0,
        "stop_price":180.0,"take_profit_price":220.0,"trailing_stop":195.0,"highest_price":210.0}]


def test_live_quotes_only_request_open_positions_and_pnl_is_display_only():
    original=positions(); spy=QuoteSpy({"AAA.IS":market_result("AAA.IS",110),"BBB.IS":market_result("BBB.IS",180)})
    live=refresh_live_positions(original,spy,now=NOW,refresh_seconds=30)
    assert spy.calls==[["AAA.IS","BBB.IS"]]
    assert live[0]["live_market_value"]==1100 and live[0]["live_unrealized_pnl"]==100
    assert live[0]["live_unrealized_pnl_pct"]==pytest.approx(10)
    assert live[1]["live_unrealized_pnl"]==-100 and live[1]["live_unrealized_pnl_pct"]==pytest.approx(-10)
    assert live[0]["price_status"]=="LIVE / FRESH"
    # Dashboard calculations cannot move persisted execution levels.
    assert live[0]["stop_price"]==original[0]["stop_price"]
    assert live[0]["trailing_stop"]==original[0]["trailing_stop"]
    assert live[0]["highest_price"]==original[0]["highest_price"]


def test_quote_failure_is_clearly_stale_fallback():
    original=positions()[:1]; spy=QuoteSpy({"AAA.IS":market_result("AAA.IS",0,available=False)})
    live=refresh_live_positions(original,spy,now=NOW)[0]
    assert live["live_price"]==98.0 and live["uses_persisted_fallback"] is True
    assert live["price_status"]=="STALE FALLBACK" and live["price_timestamp"] is None


def test_provider_timestamp_controls_delayed_and_stale_status():
    item=positions()[:1]
    delayed=refresh_live_positions(item,QuoteSpy({"AAA.IS":market_result("AAA.IS",101,NOW-timedelta(minutes=5))}),now=NOW)[0]
    stale=refresh_live_positions(item,QuoteSpy({"AAA.IS":market_result("AAA.IS",101,NOW-timedelta(hours=2))}),now=NOW)[0]
    assert delayed["price_status"]=="DELAYED" and stale["price_status"]=="STALE"
    assert delayed["price_age"]=="5 min 0 sec"


def test_live_equity_and_return_use_live_values():
    live=refresh_live_positions(positions(),QuoteSpy({"AAA.IS":market_result("AAA.IS",110),
        "BBB.IS":market_result("BBB.IS",180)}),now=NOW)
    summary=live_portfolio_summary(cash=1000,positions=live,initial_capital=3000,bot_recorded_equity=2900)
    assert summary["live_equity"]==3000 and summary["live_unrealized"]==0
    assert summary["live_return_pct"]==0 and summary["bot_recorded_equity"]==2900


def test_empty_portfolio_does_not_call_provider():
    spy=QuoteSpy({})
    assert refresh_live_positions([],spy,now=NOW)==[] and spy.calls==[]
