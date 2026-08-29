from datetime import datetime
from zoneinfo import ZoneInfo
import sqlite3
import pytest

from bistbot.app.config import load_settings
from bistbot.app.models import Action, NewsItem, RiskDecision, RiskOutcome, RiskReasonCode, TradeSignal
from bistbot.broker.base import RealBroker
from bistbot.broker.paper import PaperBroker
from bistbot.intelligence.llm_provider import validate_analysis
from bistbot.storage.database import Database
from bistbot.storage.repositories import NewsRepository


def signal(action: Action, price: float = 100) -> TradeSignal:
    return TradeSignal(symbol="TEST", action=action, score=50, reason="test", strategy_version="test", requested_price=price,
        timestamp=datetime(2026,8,31,11,tzinfo=ZoneInfo("Europe/Istanbul")))


def test_config_loads():
    assert load_settings("config.v1.yaml").risk.max_open_positions == 5


def test_schema_contains_required_tables(tmp_path):
    with Database(str(tmp_path/"test.db")) as db:
        tables = {row[0] for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"symbols","market_snapshots","technical_signals","news_items","kap_items","llm_analyses",
            "trade_signals","risk_decisions","orders","trades","positions","portfolio_snapshots","system_events"} <= tables


def test_duplicate_news_is_ignored(tmp_path):
    item = NewsItem(source_id="1",source="test",timestamp=datetime.now(),title="x",symbol="TEST",content_hash="same")
    with Database(str(tmp_path/"test.db")) as db:
        repo = NewsRepository(db)
        assert repo.add(item) is True
        assert repo.add(item) is False


def test_invalid_llm_response_is_hold():
    assert validate_analysis("not json", "TEST").action_bias is Action.HOLD


def test_paper_broker_accounting():
    broker = PaperBroker(10_000)
    buy = signal(Action.BUY)
    broker.buy(buy, RiskDecision(signal_id=buy.id,outcome=RiskOutcome.APPROVE,
        reason_code=RiskReasonCode.APPROVED,reason="test",approved_quantity=10))
    assert broker.cash() == 9_000
    sell = signal(Action.SELL, 110)
    broker.sell(sell, 10, RiskDecision(signal_id=sell.id,outcome=RiskOutcome.APPROVE,
        reason_code=RiskReasonCode.APPROVED,reason="test",approved_quantity=10))
    assert broker.cash() == 10_100 and broker.positions() == {}


def test_real_broker_is_impossible():
    with pytest.raises(NotImplementedError, match="disabled in V1"):
        RealBroker()
