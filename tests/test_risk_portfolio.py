from __future__ import annotations

from datetime import datetime,timedelta,timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from bistbot.app.config import load_settings
from bistbot.app.models import Action,RiskOrderRequest,RiskOutcome,RiskReasonCode
from bistbot.portfolio.service import PortfolioPosition,PortfolioService
from bistbot.risk.engine import DeterministicRiskEngine,GlobalKillSwitch
from bistbot.storage.database import Database
from bistbot.storage.repositories import RiskDecisionRepository,SystemStateRepository


NOW=datetime(2026,8,21,12,0,tzinfo=timezone.utc)


@pytest.fixture
def context(tmp_path):
    database=Database(str(tmp_path/"risk.db")); state_repo=SystemStateRepository(database)
    engine=DeterministicRiskEngine(load_settings("config.v1.yaml").risk,RiskDecisionRepository(database),GlobalKillSwitch(state_repo))
    yield database,state_repo,engine
    database.close()


def portfolio(**updates):
    state=PortfolioService(200_000).snapshot({},NOW)
    return state.model_copy(update=updates)


def request(*,entry="100",stop="96",quantity=None,age=0,symbol="AAA"):
    return RiskOrderRequest(signal_id=uuid4(),symbol=symbol,action=Action.BUY,entry_price=Decimal(entry),
        stop_price=None if stop is None else Decimal(stop),price_timestamp=NOW-timedelta(minutes=age),requested_quantity=quantity)


def test_decimal_portfolio_accounting_realized_unrealized_equity_and_drawdown():
    service=PortfolioService()
    assert service.cash==Decimal("200000.00")
    service.record_buy("AAA",100,"100",NOW)
    marked=service.snapshot({"AAA":"110"},NOW)
    assert marked.cash==Decimal("190000.00") and marked.unrealized_pnl==Decimal("1000.00")
    assert marked.equity==Decimal("201000.00") and marked.daily_pnl==Decimal("1000.00")
    assert marked.peak_equity==Decimal("201000.00")
    assert service.record_sell("AAA",50,"110",NOW)==Decimal("500.00")
    lower=service.snapshot({"AAA":"90"},NOW+timedelta(minutes=1))
    assert lower.realized_pnl==Decimal("500.00") and lower.unrealized_pnl==Decimal("-500.00")
    assert lower.equity==Decimal("200000.00") and lower.daily_pnl==Decimal("-1000.00")
    assert lower.drawdown_pct>0


def test_new_day_and_week_use_previous_equity_baseline():
    service=PortfolioService(); service.snapshot({},NOW)
    service.record_buy("AAA",100,"100",NOW)
    service.snapshot({"AAA":"110"},NOW+timedelta(hours=1))
    next_day=service.snapshot({"AAA":"105"},NOW+timedelta(days=1))
    assert next_day.daily_pnl==Decimal("-500.00")


def test_position_sizing_uses_equity_stop_allocation_and_trade_risk(context):
    _,_,engine=context
    decision=engine.evaluate(request(),portfolio(),NOW)
    assert decision.outcome is RiskOutcome.APPROVE and decision.approved_quantity==250


def test_requested_quantity_is_reduced_by_trade_risk(context):
    _,_,engine=context
    decision=engine.evaluate(request(quantity=400),portfolio(),NOW)
    assert decision.outcome is RiskOutcome.REDUCE_SIZE
    assert decision.reason_code is RiskReasonCode.MAX_TRADE_RISK and decision.approved_quantity==250


def test_max_position_allocation_reduces_size(context):
    _,_,engine=context
    decision=engine.evaluate(request(stop="99",quantity=500),portfolio(),NOW)
    assert decision.outcome is RiskOutcome.REDUCE_SIZE
    assert decision.reason_code is RiskReasonCode.MAX_POSITION_SIZE and decision.approved_quantity==300


def test_minimum_cash_reserve_rejects_when_no_spendable_cash(context):
    _,_,engine=context
    decision=engine.evaluate(request(),portfolio(cash=Decimal("30000")),NOW)
    assert decision.outcome is RiskOutcome.REJECT and decision.reason_code is RiskReasonCode.INSUFFICIENT_CASH


def test_maximum_positions_boundary(context):
    _,_,engine=context
    positions={f"S{i}":PortfolioPosition(symbol=f"S{i}",quantity=1,average_price=Decimal("100"),
        last_price=Decimal("100"),opened_at=NOW,updated_at=NOW) for i in range(5)}
    decision=engine.evaluate(request(),portfolio(positions=positions),NOW)
    assert decision.reason_code is RiskReasonCode.MAX_POSITIONS


@pytest.mark.parametrize(("updates","code"),[
    ({"daily_pnl":Decimal("-4000")},RiskReasonCode.DAILY_LOSS_LIMIT),
    ({"weekly_pnl":Decimal("-10000")},RiskReasonCode.WEEKLY_LOSS_LIMIT),
    ({"drawdown_pct":Decimal("0.10")},RiskReasonCode.MAX_DRAWDOWN),
])
def test_loss_boundaries_halt_trading(context,updates,code):
    _,_,engine=context
    decision=engine.evaluate(request(),portfolio(**updates),NOW)
    assert decision.outcome is RiskOutcome.HALT_TRADING and decision.reason_code is code


def test_invalid_stop_is_rejected(context):
    _,_,engine=context
    decision=engine.evaluate(request(stop="100"),portfolio(),NOW)
    assert decision.reason_code is RiskReasonCode.INVALID_STOP


def test_stale_price_is_rejected(context):
    _,_,engine=context
    decision=engine.evaluate(request(age=6),portfolio(),NOW)
    assert decision.reason_code is RiskReasonCode.STALE_PRICE


def test_global_kill_switch_is_durable_and_blocks_buys(context):
    database,state_repo,engine=context; engine.kill_switch.activate()
    assert GlobalKillSwitch(SystemStateRepository(database)).active
    decision=engine.evaluate(request(),portfolio(),NOW)
    assert decision.outcome is RiskOutcome.HALT_TRADING and decision.reason_code is RiskReasonCode.KILL_SWITCH
    engine.kill_switch.deactivate(); assert not state_repo.get("global_kill_switch")=="true"


def test_every_risk_decision_is_persisted(context):
    database,_,engine=context
    decision=engine.evaluate(request(),portfolio(),NOW)
    row=database.query("SELECT * FROM risk_decision_records")[0]
    assert row["signal_id"]==str(decision.signal_id) and row["outcome"]=="APPROVE"

