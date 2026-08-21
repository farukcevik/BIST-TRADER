from __future__ import annotations

from datetime import datetime,timedelta,timezone
from decimal import Decimal

import pytest

from bistbot.app.config import load_settings
from bistbot.app.models import Action,RiskDecision,RiskOutcome,RiskReasonCode,TradeSignal
from bistbot.broker.paper import PaperBroker
from bistbot.notifications.base import SafeNotificationDispatcher
from bistbot.notifications.email import EmailNotificationProvider
from bistbot.notifications.telegram import TelegramNotificationProvider
from bistbot.storage.database import Database


NOW=datetime(2026,8,21,12,0,tzinfo=timezone.utc)


class Recorder:
    def __init__(self): self.events=[]
    def send(self,event,message): self.events.append((event,message))


class Failure:
    def send(self,event,message): raise RuntimeError("notification down")


def signal(action=Action.BUY,price=100,reason="entry",timestamp=NOW,symbol="AAA"):
    return TradeSignal(symbol=symbol,action=action,score=77,reason=reason,strategy_version="v-test",
                       requested_price=price,timestamp=timestamp)


def approved(item,quantity):
    return RiskDecision(signal_id=item.id,outcome=RiskOutcome.APPROVE,reason_code=RiskReasonCode.APPROVED,
                        reason="approved",approved_quantity=quantity)


def buy_position(broker,*,timestamp=NOW,symbol="AAA",quantity=10):
    item=signal(timestamp=timestamp,symbol=symbol); return broker.buy(item,approved(item,quantity))


def test_commission_slippage_orders_fills_and_required_fields(tmp_path):
    database=Database(str(tmp_path/"paper.db")); recorder=Recorder()
    broker=PaperBroker(10_000,.001,.001,database=database,notifier=SafeNotificationDispatcher([recorder]))
    order=buy_position(broker)
    assert order.fill_price==Decimal("100.1000") and broker.get_cash()==Decimal("8998.00")
    assert order.signal_id and order.symbol=="AAA" and order.side is Action.BUY and order.quantity==10
    assert order.reason=="entry" and order.final_score==77 and order.risk_decision.approved
    assert order.strategy_version=="v-test" and order.timestamp==NOW
    assert database.query("SELECT COUNT(*) n FROM paper_orders")[0]["n"]==1
    assert database.query("SELECT COUNT(*) n FROM paper_fills")[0]["n"]==1
    assert recorder.events[0][0]=="BUY"
    database.close()


def test_sell_accounting_and_restart_persistence(tmp_path):
    database=Database(str(tmp_path/"paper.db")); broker=PaperBroker(10_000,.001,.001,database=database)
    buy_position(broker); item=signal(Action.SELL,110,"strategy exit")
    broker.sell(item,10,approved(item,10))
    assert broker.get_cash()==Decimal("10095.80") and broker.get_positions()=={}
    restarted=PaperBroker(999_999,.001,.001,database=database)
    assert restarted.get_cash()==Decimal("10095.80") and len(restarted.get_orders())==2
    database.close()


def test_rejected_buy_never_creates_order_and_notifies_important_rejection(tmp_path):
    database=Database(str(tmp_path/"paper.db")); recorder=Recorder()
    broker=PaperBroker(10_000,database=database,notifier=SafeNotificationDispatcher([recorder]))
    item=signal(); rejection=RiskDecision(signal_id=item.id,outcome=RiskOutcome.HALT_TRADING,
        reason_code=RiskReasonCode.MAX_DRAWDOWN,reason="halt",approved_quantity=0)
    with pytest.raises(PermissionError): broker.buy(item,rejection)
    assert broker.get_orders()==[]
    assert [event for event,_ in recorder.events]==["RISK REJECTION","TRADING HALTED"]


@pytest.mark.parametrize(("price","strategy","age_days","expected"),[
    (Decimal("95"),False,0,"STOP LOSS"),
    (Decimal("109"),False,0,"TAKE PROFIT"),
    (Decimal("100"),True,0,"STRATEGY EXIT"),
    (Decimal("100"),False,21,"TIME STOP"),
])
def test_position_exit_types(tmp_path,price,strategy,age_days,expected):
    database=Database(str(tmp_path/f"{expected}.db")); recorder=Recorder()
    broker=PaperBroker(10_000,database=database,risk_settings=load_settings().risk,
                       notifier=SafeNotificationDispatcher([recorder]))
    buy_position(broker,timestamp=NOW-timedelta(days=age_days))
    exits=broker.run_exit_checks({"AAA":price},strategy_exit_symbols={"AAA"} if strategy else set(),now=NOW)
    assert len(exits)==1 and exits[0].reason==expected and not broker.get_positions()
    assert "SELL" in [event for event,_ in recorder.events] and expected in [event for event,_ in recorder.events]


def test_trailing_stop_uses_persisted_high_water_mark(tmp_path):
    database=Database(str(tmp_path/"trail.db")); broker=PaperBroker(10_000,database=database,risk_settings=load_settings().risk)
    buy_position(broker)
    assert broker.run_exit_checks({"AAA":Decimal("107")},now=NOW+timedelta(minutes=1))==[]
    exits=broker.run_exit_checks({"AAA":Decimal("103")},now=NOW+timedelta(minutes=2))
    assert exits[0].reason=="TRAILING STOP"


def test_failing_notification_provider_cannot_stop_fill(tmp_path):
    database=Database(str(tmp_path/"notify.db")); recorder=Recorder()
    broker=PaperBroker(10_000,database=database,notifier=SafeNotificationDispatcher([Failure(),recorder]))
    buy_position(broker)
    assert broker.get_positions()["AAA"].quantity==10 and recorder.events[0][0]=="BUY"


def test_system_error_and_daily_summary_notifications():
    recorder=Recorder(); dispatcher=SafeNotificationDispatcher([recorder])
    dispatcher.system_error("boom"); dispatcher.daily_summary("summary")
    assert [event for event,_ in recorder.events]==["SYSTEM ERROR","DAILY SUMMARY"]


def test_optional_notification_adapters_are_noop_without_environment(monkeypatch):
    for key in ("TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID","SMTP_HOST","SMTP_USERNAME","SMTP_PASSWORD","SMTP_RECIPIENT"):
        monkeypatch.delenv(key,raising=False)
    TelegramNotificationProvider().send("BUY","message")
    EmailNotificationProvider().send("BUY","message")


def test_cancel_filled_order_returns_false(tmp_path):
    database=Database(str(tmp_path/"cancel.db")); broker=PaperBroker(10_000,database=database)
    order=buy_position(broker)
    assert broker.cancel_order(str(order.id)) is False

