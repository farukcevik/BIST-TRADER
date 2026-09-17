from __future__ import annotations

from datetime import datetime,timedelta,timezone
from decimal import Decimal
from threading import Barrier,Thread

import pytest

from bistbot.app.config import load_settings
from bistbot.app.models import Action,RiskDecision,RiskOutcome,RiskReasonCode,TradeSignal
from bistbot.broker.paper import PaperBroker
from bistbot.app.runtime import _apply_buy_tier_sizing
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


def approved_with_plan(item,quantity,*,target_3=130,stop=94):
    decision=approved(item,quantity)
    return decision.model_copy(update={"metadata":{"stop_price":str(stop),"target_1":"110",
        "target_2":"120","target_3":str(target_3) if target_3 is not None else None}})


def buy_position(broker,*,timestamp=NOW,symbol="AAA",quantity=10):
    broker.clock=lambda:timestamp
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
    broker=PaperBroker(10_000,database=database,risk_settings=load_settings("config.v1.yaml").risk,
                       notifier=SafeNotificationDispatcher([recorder]))
    buy_position(broker,timestamp=NOW-timedelta(days=age_days))
    exits=broker.run_exit_checks({"AAA":price},strategy_exit_symbols={"AAA"} if strategy else set(),now=NOW)
    assert len(exits)==1 and exits[0].reason==expected and not broker.get_positions()
    assert "SELL" in [event for event,_ in recorder.events] and expected in [event for event,_ in recorder.events]


def test_trailing_stop_uses_persisted_high_water_mark(tmp_path):
    database=Database(str(tmp_path/"trail.db")); broker=PaperBroker(10_000,database=database,risk_settings=load_settings("config.v1.yaml").risk)
    buy_position(broker)
    assert broker.run_exit_checks({"AAA":Decimal("107")},now=NOW+timedelta(minutes=1))==[]
    exits=broker.run_exit_checks({"AAA":Decimal("103")},now=NOW+timedelta(minutes=2))
    assert exits[0].reason=="TRAILING STOP"


def test_multi_target_partial_exits_persist_across_restart(tmp_path):
    database=Database(str(tmp_path/"targets.db")); settings=load_settings("config.v1.yaml").risk
    broker=PaperBroker(10_000,database=database,risk_settings=settings,clock=lambda:NOW)
    item=signal(); broker.buy(item,approved_with_plan(item,10))

    first=broker.run_exit_checks({"AAA":Decimal("110")},now=NOW+timedelta(minutes=1))
    assert [(order.reason,order.quantity) for order in first]==[("TARGET 1",2)]
    assert broker.get_positions()["AAA"].quantity==8

    restarted=PaperBroker(10_000,database=database,risk_settings=settings,clock=lambda:NOW)
    second=restarted.run_exit_checks({"AAA":Decimal("120")},now=NOW+timedelta(minutes=2))
    assert [(order.reason,order.quantity) for order in second]==[("TARGET 2",2)]
    assert restarted.get_positions()["AAA"].quantity==6
    assert restarted.run_exit_checks({"AAA":Decimal("120")},now=NOW+timedelta(minutes=3))==[]

    final=restarted.run_exit_checks({"AAA":Decimal("130")},now=NOW+timedelta(minutes=4))
    assert [(order.reason,order.quantity) for order in final]==[("TARGET 3",6)]
    assert restarted.get_positions()=={}


def test_concurrent_target_checks_fill_tranche_once_and_persist_marker(tmp_path):
    path=tmp_path/"concurrent-target.db"; settings=load_settings("config.v1.yaml").risk
    seed=Database(str(path)); broker=PaperBroker(10_000,database=seed,risk_settings=settings,clock=lambda:NOW)
    item=signal(); broker.buy(item,approved_with_plan(item,8)); seed.close()
    barrier=Barrier(2); orders=[]

    def check_target():
        database=Database(str(path)); concurrent=PaperBroker(10_000,database=database,
            risk_settings=settings,clock=lambda:NOW)
        barrier.wait()
        orders.extend(concurrent.run_exit_checks({"AAA":Decimal("110")},now=NOW+timedelta(minutes=1)))
        database.close()

    threads=[Thread(target=check_target),Thread(target=check_target)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert [(order.reason,order.quantity) for order in orders]==[("TARGET 1",2)]

    database=Database(str(path)); restarted=PaperBroker(10_000,database=database,risk_settings=settings)
    assert restarted.get_positions()["AAA"].quantity==6
    assert database.query("SELECT target_1_hit FROM paper_positions WHERE symbol='AAA'")[0]["target_1_hit"]==1
    assert database.query("SELECT COUNT(*) n FROM paper_orders WHERE reason='TARGET 1'")[0]["n"]==1


def test_concurrent_multi_level_checks_execute_each_target_once_without_errors(tmp_path):
    path=tmp_path/"concurrent-all-targets.db"; settings=load_settings("config.v1.yaml").risk
    seed=Database(str(path)); broker=PaperBroker(10_000,database=seed,risk_settings=settings,clock=lambda:NOW)
    item=signal(); broker.buy(item,approved_with_plan(item,8)); seed.close()
    barrier=Barrier(2); orders=[]; errors=[]

    def check_targets():
        database=Database(str(path)); concurrent=PaperBroker(10_000,database=database,
            risk_settings=settings,clock=lambda:NOW)
        barrier.wait()
        try:
            orders.extend(concurrent.run_exit_checks({"AAA":Decimal("130")},now=NOW+timedelta(minutes=1)))
        except Exception as error:
            errors.append(error)
        finally:
            database.close()

    threads=[Thread(target=check_targets),Thread(target=check_targets)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads) and errors==[]
    assert sorted((order.reason,order.quantity) for order in orders)==[
        ("TARGET 1",2),("TARGET 2",2),("TARGET 3",4)]
    database=Database(str(path))
    assert database.query("SELECT COUNT(*) n FROM paper_positions")[0]["n"]==0
    assert database.query("SELECT COUNT(*) n FROM paper_orders WHERE side='SELL'")[0]["n"]==3


def test_missing_t3_leaves_remainder_to_existing_trailing_stop(tmp_path):
    database=Database(str(tmp_path/"targets-trailing.db")); settings=load_settings("config.v1.yaml").risk
    broker=PaperBroker(10_000,database=database,risk_settings=settings,clock=lambda:NOW)
    item=signal(); broker.buy(item,approved_with_plan(item,8,target_3=None))
    exits=broker.run_exit_checks({"AAA":Decimal("120")},now=NOW+timedelta(minutes=1))
    assert [(order.reason,order.quantity) for order in exits]==[("TARGET 1",2),("TARGET 2",2)]
    assert broker.get_positions()["AAA"].quantity==4

    exits=broker.run_exit_checks({"AAA":Decimal("115")},now=NOW+timedelta(minutes=2))
    assert [(order.reason,order.quantity) for order in exits]==[("TRAILING STOP",4)]
    assert broker.get_positions()=={}


def test_persisted_structure_stop_overrides_default_stop(tmp_path):
    database=Database(str(tmp_path/"structure-stop.db")); settings=load_settings("config.v1.yaml").risk
    broker=PaperBroker(10_000,database=database,risk_settings=settings,clock=lambda:NOW)
    item=signal(); broker.buy(item,approved_with_plan(item,8,stop=97))
    exits=broker.run_exit_checks({"AAA":Decimal("97")},now=NOW+timedelta(minutes=1))
    assert [(order.reason,order.quantity) for order in exits]==[("STOP LOSS",8)]


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


def test_duplicate_buy_is_rejected_without_pyramiding(tmp_path):
    database=Database(str(tmp_path/"duplicate.db")); broker=PaperBroker(10_000,database=database)
    buy_position(broker); duplicate=signal()
    with pytest.raises(ValueError,match="pyramiding disabled"):
        broker.buy(duplicate,approved(duplicate,1))
    assert broker.get_positions()["AAA"].quantity==10
    assert len(broker.get_orders())==1


def test_concurrent_duplicate_buy_is_rejected_inside_locked_transaction(tmp_path):
    path=tmp_path/"concurrent-duplicate.db"
    seed=Database(str(path)); PaperBroker(10_000,database=seed); seed.close()
    barrier=Barrier(2); outcomes=[]

    def buy_once():
        database=Database(str(path)); broker=PaperBroker(10_000,database=database,clock=lambda:NOW)
        item=signal()
        barrier.wait()
        try:
            broker.buy(item,approved(item,10)); outcomes.append("FILLED")
        except ValueError as error:
            outcomes.append(str(error))
        finally:
            database.close()

    threads=[Thread(target=buy_once),Thread(target=buy_once)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes)==["FILLED","existing paper position; pyramiding disabled"]
    database=Database(str(path)); broker=PaperBroker(10_000,database=database)
    assert broker.get_cash()==Decimal("9000.00")
    assert broker.get_positions()["AAA"].quantity==10
    assert len(broker.get_orders())==1
    database.close()


def test_flex_quantity_is_half_risk_approved_quantity_rounded_down():
    item=signal(); decision=approved(item,11)
    flex=_apply_buy_tier_sizing(decision,buy_tier="FLEX",multiplier=.5)
    assert flex.approved_quantity==5
    assert flex.metadata=={"buy_tier":"FLEX","normal_approved_quantity":11,
                           "buy_tier_position_multiplier":"0.5"}
    assert decision.approved_quantity==11


def test_zero_share_flex_runtime_sizing_holds_without_broker_mutation(tmp_path):
    database=Database(str(tmp_path/"zero-flex.db"))
    broker=PaperBroker(10_000,database=database,clock=lambda:NOW)
    item=signal().model_copy(update={"buy_tier":"FLEX"})
    cash_before=broker.get_cash()

    risk_decision=_apply_buy_tier_sizing(approved(item,1),buy_tier="FLEX",multiplier=.5)
    action=Action.HOLD if risk_decision.approved_quantity<=0 else item.action
    if action is Action.BUY:
        broker.buy(item,risk_decision)

    assert action is Action.HOLD
    assert risk_decision.approved_quantity==0
    assert broker.get_cash()==cash_before
    assert broker.get_positions()=={} and broker.get_orders()==[]
    assert database.query("SELECT COUNT(*) n FROM paper_orders")[0]["n"]==0
    assert database.query("SELECT COUNT(*) n FROM paper_fills")[0]["n"]==0
    database.close()


def test_flex_fill_persists_tier_on_order_fill_and_exit(tmp_path):
    database=Database(str(tmp_path/"flex.db")); broker=PaperBroker(10_000,database=database,clock=lambda:NOW)
    buy=signal().model_copy(update={"buy_tier":"FLEX"})
    order=broker.buy(buy,approved(buy,5))
    assert order.buy_tier=="FLEX" and broker.get_orders()[0].buy_tier=="FLEX"
    assert database.query("SELECT buy_tier FROM paper_orders")[0]["buy_tier"]=="FLEX"
    assert database.query("SELECT buy_tier FROM paper_fills")[0]["buy_tier"]=="FLEX"

    sell=signal(timestamp=NOW+timedelta(minutes=1)).model_copy(update={"action":Action.SELL})
    exit_order=broker.sell(sell,5,approved(sell,5))
    assert exit_order.buy_tier=="FLEX"
    tiers=[row["buy_tier"] for row in database.query("SELECT buy_tier FROM paper_fills ORDER BY timestamp")]
    assert tiers==["FLEX","FLEX"]
