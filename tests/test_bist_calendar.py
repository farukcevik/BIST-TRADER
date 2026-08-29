from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from bistbot.app.config import load_settings
from bistbot.app.models import Action, RiskDecision, RiskOutcome, RiskReasonCode, TradeSignal
from bistbot.broker.paper import PaperBroker
from bistbot.market.calendar import BistTradingCalendar, MarketClosedError, MarketSession
from bistbot.storage.database import Database

IST=ZoneInfo("Europe/Istanbul")

def at(year,month,day,hour,minute=0): return datetime(year,month,day,hour,minute,tzinfo=IST)

def signal(action,when,price=100):
    return TradeSignal(symbol="AAA",action=action,score=80,reason="test",strategy_version="test",
        requested_price=price,timestamp=when)

def approved(item,quantity=10):
    return RiskDecision(signal_id=item.id,outcome=RiskOutcome.APPROVE,reason_code=RiskReasonCode.APPROVED,
        reason="approved",approved_quantity=quantity)

@pytest.mark.parametrize("when",[at(2026,8,29,12),at(2026,8,30,12)])
def test_weekend_blocks_broker_buy_without_mutation(tmp_path,when):
    db=Database(str(tmp_path/"paper.db")); broker=PaperBroker(10_000,database=db)
    item=signal(Action.BUY,when); cash=broker.get_cash()
    with pytest.raises(MarketClosedError,match="MARKET_CLOSED_WEEKEND"):
        broker.buy(item,approved(item))
    assert broker.get_cash()==cash and broker.get_positions()=={} and broker.get_orders()==[]

@pytest.mark.parametrize("when,session,executable",[
    (at(2026,8,31,9,45),MarketSession.PRE_OPEN,False),
    (at(2026,8,31,9,57),MarketSession.OPENING_AUCTION,False),
    (at(2026,8,31,10,30),MarketSession.CONTINUOUS_TRADING,True),
    (at(2026,8,31,18,5),MarketSession.CLOSING_AUCTION,False),
    (at(2026,8,31,18,30),MarketSession.POST_CLOSE,False),
])
def test_normal_trading_day_session_execution_policy(when,session,executable):
    status=BistTradingCalendar().status(when)
    assert status.current_session is session and status.can_execute_orders is executable

def test_official_full_day_holiday_and_half_day_close_are_blocked():
    calendar=BistTradingCalendar()
    assert calendar.status(at(2026,10,29,11)).reason=="MARKET_CLOSED_HOLIDAY"
    half=calendar.status(at(2026,10,28,13,0))
    assert half.is_half_day and half.current_session is MarketSession.POST_CLOSE and not half.can_execute_orders

def test_istanbul_timezone_conversion_is_authoritative():
    utc=datetime(2026,8,31,7,30,tzinfo=timezone.utc)
    status=BistTradingCalendar().status(utc)
    assert status.local_time.hour==10 and status.local_time.minute==30 and status.can_execute_orders

def test_closed_market_blocks_sell_and_all_price_exits(tmp_path):
    db=Database(str(tmp_path/"paper.db")); settings=load_settings("config.yaml")
    broker=PaperBroker(10_000,database=db,risk_settings=settings.risk)
    friday=at(2026,8,28,12); buy=signal(Action.BUY,friday)
    broker.buy(buy,approved(buy))
    cash=broker.get_cash(); position=broker.get_positions()["AAA"]
    saturday=at(2026,8,29,12); sell=signal(Action.SELL,saturday,90)
    with pytest.raises(MarketClosedError,match="MARKET_CLOSED_WEEKEND"):
        broker.sell(sell,10,approved(sell))
    for price in (Decimal("94"),Decimal("111")):
        assert broker.run_exit_checks({"AAA":price},now=saturday)==[]
    assert broker.get_cash()==cash and broker.get_positions()["AAA"]==position

def test_closed_signal_is_not_persisted_or_automatically_replayed(tmp_path):
    db=Database(str(tmp_path/"paper.db")); broker=PaperBroker(10_000,database=db)
    weekend=signal(Action.BUY,at(2026,8,29,12))
    with pytest.raises(MarketClosedError): broker.buy(weekend,approved(weekend))
    assert broker.get_orders()==[]
    monday=signal(Action.BUY,at(2026,8,31,11))
    broker.buy(monday,approved(monday))
    assert len(broker.get_orders())==1 and broker.get_orders()[0].signal_id==monday.id

def test_unsupported_calendar_year_fails_closed():
    assert BistTradingCalendar().status(at(2027,1,4,12)).reason=="MARKET_CALENDAR_UNAVAILABLE"

def test_friday_close_is_not_an_executable_monday_open_price(tmp_path):
    db=Database(str(tmp_path/"paper.db")); broker=PaperBroker(10_000,database=db)
    stale=signal(Action.BUY,at(2026,8,28,17,59))
    with pytest.raises(MarketClosedError,match="NO_FRESH_SESSION_PRICE"):
        broker.buy(stale,approved(stale),execution_time=at(2026,8,31,10,1))
    assert broker.get_orders()==[] and broker.get_positions()=={}
