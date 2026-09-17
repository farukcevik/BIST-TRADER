from datetime import datetime,timezone
from decimal import Decimal
from uuid import uuid4
import yaml
from threading import Barrier,Thread

import pytest

from bistbot.app.config import Settings,load_settings
from bistbot.app.models import Action,RiskOrderRequest,RiskReasonCode
from bistbot.broker.paper import PaperBroker
from bistbot.portfolio.controls import PaperPortfolioControlService
from bistbot.portfolio.service import PortfolioPosition,PortfolioService
from bistbot.risk.engine import DeterministicRiskEngine,GlobalKillSwitch
from bistbot.storage.database import Database
from bistbot.storage.repositories import RiskDecisionRepository,SystemStateRepository

NOW=datetime(2026,8,31,12,tzinfo=timezone.utc)

def service(database,configured=5):
    PaperBroker(10_000,database=database,clock=lambda:NOW)
    return PaperPortfolioControlService(database,mode="PAPER",configured_max_open_positions=configured)

def test_controls_fail_closed_outside_paper(tmp_path):
    database=Database(str(tmp_path/"mode.db"))
    with pytest.raises(PermissionError,match="PAPER_CONTROLS_MODE_REQUIRED"):
        PaperPortfolioControlService(database,mode="LIVE",configured_max_open_positions=5)
    payload=yaml.safe_load(open("config.v1.yaml",encoding="utf-8"))
    payload["trading_mode"]="LIVE"
    with pytest.raises(ValueError): Settings.model_validate(payload)

def test_absolute_cash_update_is_cas_idempotent_and_audited(tmp_path):
    database=Database(str(tmp_path/"cash.db")); controls=service(database)
    state=controls.set_cash("1234.56",expected_version=0,idempotency_token="cash-1",actor="test")
    assert state.cash==Decimal("1234.56") and state.cash_version==1
    replay=controls.set_cash("1234.56",expected_version=0,idempotency_token="cash-1",actor="test")
    assert replay==state
    with pytest.raises(RuntimeError,match="STALE_CONTROL_VERSION"):
        controls.set_cash(2000,expected_version=0,idempotency_token="cash-2")
    with pytest.raises(ValueError,match="IDEMPOTENCY_TOKEN_REUSE"):
        controls.set_cash(999,expected_version=1,idempotency_token="cash-1")
    assert database.query("SELECT COUNT(*) n FROM system_events WHERE event_type='PAPER_PORTFOLIO_CONTROL'")[0]["n"]==1

def test_paper_broker_exposes_guarded_absolute_cash_api(tmp_path):
    database=Database(str(tmp_path/"broker-cash.db")); settings=load_settings("config.yaml")
    broker=PaperBroker(10_000,database=database,risk_settings=settings.risk,clock=lambda:NOW)
    result=broker.set_cash(2500,expected_version=0,idempotency_token="broker-cash",actor="test")
    assert result.cash==broker.get_cash()==Decimal("2500") and result.cash_version==1

def test_cash_compare_and_swap_allows_only_one_concurrent_writer(tmp_path):
    path=tmp_path/"cash-race.db"; seed=Database(str(path)); service(seed); seed.close()
    barrier=Barrier(2); outcomes=[]
    def update(amount,token):
        database=Database(str(path)); controls=PaperPortfolioControlService(database,mode="PAPER",configured_max_open_positions=5)
        barrier.wait()
        try: controls.set_cash(amount,expected_version=0,idempotency_token=token); outcomes.append("UPDATED")
        except RuntimeError as error: outcomes.append(str(error))
        finally: database.close()
    threads=[Thread(target=update,args=(1000,"race-a")),Thread(target=update,args=(2000,"race-b"))]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=5)
    assert sorted(outcomes)==["STALE_CONTROL_VERSION","UPDATED"]

@pytest.mark.parametrize("value",[0,21,-1,True,1.5])
def test_max_open_positions_range_is_one_to_twenty(tmp_path,value):
    controls=service(Database(str(tmp_path/f"range-{value}.db")))
    with pytest.raises(ValueError): controls.set_max_open_positions(value,expected_version=0,idempotency_token=str(value))

def test_max_override_and_reset_fall_back_to_config_without_liquidation(tmp_path):
    database=Database(str(tmp_path/"max.db")); controls=service(database,configured=5)
    database.execute("INSERT INTO paper_positions(symbol,quantity,average_price,last_price,high_price,opened_at,updated_at,position_status) VALUES(?,?,?,?,?,?,?,?)",
        ("AAA",1,"100","100","100",NOW.isoformat(),NOW.isoformat(),"FRESH"))
    overridden=controls.set_max_open_positions(2,expected_version=0,idempotency_token="max-1")
    assert overridden.effective_max_open_positions==2 and overridden.max_open_positions_version==1
    assert len(database.query("SELECT * FROM paper_positions"))==1
    reset=controls.reset_max_open_positions(expected_version=1,idempotency_token="max-reset")
    assert reset.max_open_positions_override is None and reset.effective_max_open_positions==5
    assert reset.max_open_positions_version==2
    assert len(database.query("SELECT * FROM paper_positions"))==1

def test_invalid_persisted_override_fails_closed(tmp_path):
    database=Database(str(tmp_path/"invalid-setting.db")); controls=service(database)
    database.execute("INSERT INTO runtime_settings VALUES(?,?,?,?)",
        ("paper.max_open_positions","99",NOW.isoformat(),1))
    with pytest.raises(RuntimeError,match="INVALID_RUNTIME_SETTING"): controls.state()

def test_effective_limit_blocks_buy_with_distinct_reason_but_sell_continues(tmp_path):
    database=Database(str(tmp_path/"risk.db")); settings=load_settings("config.yaml"); controls=service(database,configured=5)
    controls.set_max_open_positions(1,expected_version=0,idempotency_token="max")
    effective=controls.effective_risk_settings(settings.risk)
    engine=DeterministicRiskEngine(effective,RiskDecisionRepository(database),GlobalKillSwitch(SystemStateRepository(database)))
    position=PortfolioPosition(symbol="AAA",quantity=1,average_price=Decimal("100"),last_price=Decimal("100"),opened_at=NOW,updated_at=NOW)
    state=PortfolioService(10_000).snapshot({},NOW).model_copy(update={"positions":{"AAA":position}})
    buy=RiskOrderRequest(signal_id=uuid4(),symbol="BBB",action=Action.BUY,entry_price=Decimal("100"),
        stop_price=Decimal("95"),price_timestamp=NOW,requested_quantity=1)
    buy_decision=engine.evaluate(buy,state,NOW)
    assert buy_decision.reason_code is RiskReasonCode.MAX_OPEN_POSITIONS
    assert buy_decision.reason_code is not RiskReasonCode.INSUFFICIENT_CASH
    sell=RiskOrderRequest(signal_id=uuid4(),symbol="AAA",action=Action.SELL,entry_price=Decimal("100"),
        price_timestamp=NOW,requested_quantity=1)
    assert engine.evaluate(sell,state,NOW).approved
