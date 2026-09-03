from datetime import datetime,timedelta,timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from bistbot.app.config import load_settings
from bistbot.app.models import Action,QuoteFreshnessStatus,RiskOrderRequest,TradeSignal
from bistbot.broker.paper import PaperBroker
from bistbot.market.calendar import BistTradingCalendar
from bistbot.market.execution_policy import validate_execution_quote
from bistbot.portfolio.service import PortfolioService
from bistbot.risk.engine import DeterministicRiskEngine,GlobalKillSwitch
from bistbot.storage.database import Database
from bistbot.storage.repositories import RiskDecisionRepository,SystemStateRepository

NOW=datetime(2026,8,31,12,0,tzinfo=timezone.utc)

def validation(age,provider="YahooBistProvider",timestamp_marker=True,now=NOW,price=100):
    timestamp=now-age if timestamp_marker else None
    return validate_execution_quote(SimpleNamespace(provider=provider,price=price,source_timestamp=timestamp),
        evaluated_at=now,calendar=BistTradingCalendar(),default_freshness_seconds=300,
        yahoo_freshness_seconds=1200)

@pytest.mark.parametrize(("age","valid","status"),[
    (timedelta(minutes=4),True,QuoteFreshnessStatus.FRESH),
    (timedelta(minutes=14),True,QuoteFreshnessStatus.CURRENT_SESSION_DELAYED_ACCEPTED),
    (timedelta(minutes=19,seconds=59),True,QuoteFreshnessStatus.CURRENT_SESSION_DELAYED_ACCEPTED),
    (timedelta(minutes=20,seconds=1),False,QuoteFreshnessStatus.STALE_CURRENT_SESSION),
])
def test_yahoo_boundaries(age,valid,status):
    result=validation(age)
    assert result.valid is valid and result.freshness_status is status
    assert result.freshness_limit_seconds==1200

def test_non_yahoo_keeps_five_minute_limit():
    result=validation(timedelta(minutes=14),provider="OTHER")
    assert not result.valid and result.freshness_limit_seconds==300

@pytest.mark.parametrize(("now","stamp"),[
    (datetime(2026,8,22,12,tzinfo=timezone.utc),datetime(2026,8,22,11,55,tzinfo=timezone.utc)),
    (datetime(2026,5,19,12,tzinfo=timezone.utc),datetime(2026,5,19,11,55,tzinfo=timezone.utc)),
    (datetime(2026,8,31,16,tzinfo=timezone.utc),datetime(2026,8,31,15,minute=55,tzinfo=timezone.utc)),
    (NOW,NOW-timedelta(days=1)),
    (NOW,None),
])
def test_yahoo_rejects_weekend_holiday_previous_session_and_missing_timestamp(now,stamp):
    result=validate_execution_quote(SimpleNamespace(provider="YahooBistProvider",price=100,source_timestamp=stamp),
        evaluated_at=now,calendar=BistTradingCalendar(),default_freshness_seconds=300,yahoo_freshness_seconds=1200)
    assert not result.valid

@pytest.mark.parametrize("price",[0,-1,float("nan"),float("inf")])
def test_yahoo_rejects_nonpositive_or_nonfinite_price(price):
    assert not validation(timedelta(minutes=4),price=price).valid

def test_yahoo_15m01_buy_passes_canonical_risk_and_independent_broker(tmp_path):
    settings=load_settings("config.yaml"); database=Database(str(tmp_path/"paper.db"))
    checked=validation(timedelta(minutes=15,seconds=1))
    state=PortfolioService(10_000).snapshot({},NOW)
    engine=DeterministicRiskEngine(settings.risk,RiskDecisionRepository(database),
        GlobalKillSwitch(SystemStateRepository(database)))
    request=RiskOrderRequest(signal_id=uuid4(),symbol="AAA",action=Action.BUY,entry_price=Decimal("100"),
        stop_price=Decimal("95"),price_timestamp=checked.source_timestamp,execution_quote_validation=checked,
        requested_quantity=1)
    decision=engine.evaluate(request,state,NOW)
    signal=TradeSignal(id=request.signal_id,symbol="AAA",action=Action.BUY,score=80,reason="test",
        strategy_version="test",requested_price=100,timestamp=checked.source_timestamp)
    broker=PaperBroker(10_000,database=database,risk_settings=settings.risk,
        yahoo_execution_freshness_seconds=settings.market_data.yahoo_execution_freshness_seconds,clock=lambda:NOW)
    order=broker.buy(signal,decision,execution_time=NOW,execution_provider="YahooBistProvider")
    assert decision.approved and order.quantity==1

def test_broker_clock_advance_rejects_just_expired_yahoo_quote_without_mutation(tmp_path):
    settings=load_settings("config.yaml"); database=Database(str(tmp_path/"expired.db"))
    timestamp=NOW-timedelta(minutes=19,seconds=59)
    checked=validation(timedelta(minutes=19,seconds=59))
    engine=DeterministicRiskEngine(settings.risk,RiskDecisionRepository(database),
        GlobalKillSwitch(SystemStateRepository(database)))
    request=RiskOrderRequest(signal_id=uuid4(),symbol="AAA",action=Action.BUY,entry_price=Decimal("100"),
        stop_price=Decimal("95"),price_timestamp=timestamp,execution_quote_validation=checked,requested_quantity=1)
    decision=engine.evaluate(request,PortfolioService(10_000).snapshot({},NOW),NOW)
    broker=PaperBroker(10_000,database=database,risk_settings=settings.risk,
        yahoo_execution_freshness_seconds=settings.market_data.yahoo_execution_freshness_seconds,
        clock=lambda:NOW+timedelta(seconds=2))
    signal=TradeSignal(id=request.signal_id,symbol="AAA",action=Action.BUY,score=80,reason="test",
        strategy_version="test",requested_price=100,timestamp=timestamp)
    with pytest.raises(Exception,match="NO_FRESH_SESSION_PRICE"):
        broker.buy(signal,decision,execution_time=NOW,execution_provider="YahooBistProvider")
    assert broker.get_cash()==Decimal("10000.0000") and broker.get_positions()=={} and broker.get_orders()==[]
    assert database.query("SELECT COUNT(*) AS count FROM paper_fills")[0]["count"]==0

def test_non_yahoo_cannot_backdate_execution_past_market_close(tmp_path):
    settings=load_settings("config.yaml"); database=Database(str(tmp_path/"non-yahoo-close.db"))
    timestamp=NOW-timedelta(minutes=4)
    checked=validation(timedelta(minutes=4),provider="OTHER")
    request=RiskOrderRequest(signal_id=uuid4(),symbol="AAA",action=Action.BUY,entry_price=Decimal("100"),
        stop_price=Decimal("95"),price_timestamp=timestamp,execution_quote_validation=checked,requested_quantity=1)
    engine=DeterministicRiskEngine(settings.risk,RiskDecisionRepository(database),
        GlobalKillSwitch(SystemStateRepository(database)))
    decision=engine.evaluate(request,PortfolioService(10_000).snapshot({},NOW),NOW)
    signal=TradeSignal(id=request.signal_id,symbol="AAA",action=Action.BUY,score=80,reason="test",
        strategy_version="test",requested_price=100,timestamp=timestamp)
    broker=PaperBroker(10_000,database=database,risk_settings=settings.risk,
        yahoo_execution_freshness_seconds=settings.market_data.yahoo_execution_freshness_seconds,
        clock=lambda:NOW.replace(hour=16))
    with pytest.raises(Exception,match="MARKET_CLOSED"):
        broker.buy(signal,decision,execution_time=NOW,execution_provider="OTHER")
    assert broker.get_cash()==Decimal("10000.0000") and broker.get_positions()=={} and broker.get_orders()==[]
    assert database.query("SELECT COUNT(*) AS count FROM paper_fills")[0]["count"]==0
