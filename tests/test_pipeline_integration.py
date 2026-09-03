from datetime import datetime, timezone
from decimal import Decimal

from bistbot.app.config import load_settings
from bistbot.app.models import (
    Action,
    EventSourceType,
    LLMAnalysisInput,
    LLMCompletion,
    RiskOrderRequest,
    TradeSignal,
)
from bistbot.broker.paper import PaperBroker
from bistbot.intelligence.kap_provider import MockKapProvider
from bistbot.intelligence.llm_provider import LLMAnalyst, MockLLMProvider
from bistbot.intelligence.news_provider import MockNewsProvider
from bistbot.intelligence.ranking import EventRanker, make_event
from bistbot.market.provider import DemoMarketDataProvider, DemoTransport
from bistbot.market.scanner import DeterministicMarketScanner
from bistbot.portfolio.service import PortfolioService
from bistbot.risk.engine import DeterministicRiskEngine, GlobalKillSwitch
from bistbot.storage.database import Database
from bistbot.storage.repositories import (
    LLMAnalysisRepository,
    RiskDecisionRepository,
    SystemStateRepository,
)


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def test_stage_contracts_support_one_paper_buy(tmp_path):
    """One candidate can cross every boundary without hidden type conversion."""
    settings = load_settings("config.v1.yaml")
    provider = DemoMarketDataProvider(
        DemoTransport(lambda: NOW),
        now=lambda: NOW,
        requests_per_second=1_000_000,
    )
    market_result = provider.historical(["THYAO"], bars=60)["THYAO.IS"]
    assert market_result.available

    technical = DeterministicMarketScanner(settings.scanner).scan(
        {"THYAO.IS": market_result.candles}, now=NOW
    )[0]
    event = make_event(
        symbol="THYAO.IS",
        source="KAP",
        source_type=EventSourceType.KAP,
        title="THYAO signed a material investment contract",
        body="Board approved capacity expansion of TRY 1 billion.",
        published_at=NOW,
        fetched_at=NOW,
        trust_score=95,
    )
    ranked = EventRanker(
        settings.intelligence,
        MockNewsProvider(),
        MockKapProvider([event]),
    ).rank([technical], now=NOW)
    assert ranked[0].event_ids == [event.id]

    response = LLMCompletion(
        model_name="mock-v1",
        content=(
            '{"symbol":"THYAO.IS","sentiment":70,"importance":80,'
            '"catalyst_score":75,"priced_in_probability":30,"risk_score":25,'
            '"confidence":80,"time_horizon":"swing","action_bias":"BUY",'
            '"summary":"Material supplied event.","bull_case":"Expansion.",'
            '"bear_case":"Execution risk.","risks":["Execution"],'
            f'"source_ids":["{event.id}"]}}'
        ),
    )
    database = Database(str(tmp_path / "pipeline.db"))
    try:
        analysis = LLMAnalyst(
            MockLLMProvider([response]),
            LLMAnalysisRepository(database),
            model_name="mock-v1",
        ).analyze([LLMAnalysisInput(symbol="THYAO.IS", technical_signal=technical, events=[event])])[0]
        assert analysis.action_bias is Action.BUY

        price = Decimal(str(market_result.snapshot.price))
        signal = TradeSignal(
            symbol="THYAO.IS",
            action=Action.BUY,
            score=75,
            reason="integration fixture final score",
            strategy_version=settings.strategy_version,
            requested_price=float(price),
            timestamp=NOW,
        )
        portfolio = PortfolioService(settings.capital).snapshot({}, NOW)
        risk = DeterministicRiskEngine(
            settings.risk,
            RiskDecisionRepository(database),
            GlobalKillSwitch(SystemStateRepository(database)),
        )
        decision = risk.evaluate(
            RiskOrderRequest(
                signal_id=signal.id,
                symbol=signal.symbol,
                action=signal.action,
                entry_price=price,
                stop_price=price * Decimal("0.96"),
                price_timestamp=NOW,
            ),
            portfolio,
            NOW,
        )
        order = PaperBroker(
            settings.capital,
            settings.execution.commission_pct,
            settings.execution.slippage_pct,
                database=database,
                risk_settings=settings.risk,
                clock=lambda:NOW,
            ).buy(signal, decision)
        assert order.side is Action.BUY
        assert order.risk_decision == decision
        assert database.query("SELECT COUNT(*) AS count FROM paper_fills")[0]["count"] == 1
    finally:
        database.close()
