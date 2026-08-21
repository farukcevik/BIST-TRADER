from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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
from bistbot.market.provider import DemoMarketDataProvider
from bistbot.market.scanner import DeterministicMarketScanner
from bistbot.notifications.base import SafeNotificationDispatcher
from bistbot.portfolio.service import PortfolioService
from bistbot.risk.engine import DeterministicRiskEngine, GlobalKillSwitch
from bistbot.storage.database import Database
from bistbot.storage.repositories import (
    EventRepository,
    IntelligenceRankingRepository,
    LLMAnalysisRepository,
    RiskDecisionRepository,
    SystemStateRepository,
    TechnicalSignalRepository,
)
from bistbot.strategy.scoring import weighted_score


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


class NotificationRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def send(self, event: str, message: str) -> None:
        self.events.append((event, message))


def _analysis_json(symbol: str, source_id: str) -> str:
    return json.dumps(
        {
            "symbol": symbol,
            "sentiment": 80,
            "importance": 90,
            "catalyst_score": 88,
            "priced_in_probability": 20,
            "risk_score": 20,
            "confidence": 85,
            "time_horizon": "swing",
            "action_bias": "BUY",
            "summary": "Supplied KAP event is material and momentum supports it.",
            "bull_case": "The supplied capacity event may improve operating potential.",
            "bear_case": "The event may already be reflected in the market price.",
            "risks": ["Execution timing and priced-in risk"],
            "source_ids": [source_id],
        }
    )


def test_offline_full_pipeline_523_to_paper_fill_and_stop_exit(tmp_path):
    """A deterministic, credential-free proof that every V1 boundary composes."""
    settings = load_settings("config.v1.yaml")
    database = Database(str(tmp_path / "end-to-end.db"))
    recorder = NotificationRecorder()
    try:
        # Market data: one batch, no real sleeping or network access.
        provider = DemoMarketDataProvider(
            now=lambda: NOW,
            batch_size=523,
            requests_per_second=1_000_000,
            sleeper=lambda _seconds: None,
        )
        symbols = provider.active_symbols()
        market_results = provider.historical(symbols, bars=60)
        assert len(symbols) == len(market_results) == 523
        assert all(result.available for result in market_results.values())

        # Scanner: only normalized, available histories enter this boundary.
        histories = {
            symbol: result.candles
            for symbol, result in market_results.items()
            if result.available
        }
        scanner = DeterministicMarketScanner(
            settings.scanner, TechnicalSignalRepository(database)
        )
        top_40 = scanner.scan(histories, limit=settings.scanner_top_n, now=NOW)
        assert len(top_40) == 40

        # Enrichment: events are supplied only for scanner candidates. The KAP
        # source is intentionally deterministic and requires no external service.
        events = [
            make_event(
                symbol=signal.symbol,
                source="KAP mock",
                source_type=EventSourceType.KAP,
                title=f"{signal.symbol} capacity expansion investment approved",
                body="Board approved a TRY 100 million new facility investment.",
                published_at=NOW - timedelta(hours=index),
                fetched_at=NOW,
                trust_score=settings.intelligence.kap_trust_score,
            )
            for index, signal in enumerate(top_40[:10])
        ]
        ranker = EventRanker(
            settings.intelligence,
            MockNewsProvider(),
            MockKapProvider(events),
            EventRepository(database),
            IntelligenceRankingRepository(database),
        )
        top_10 = ranker.rank(top_40, limit=settings.analysis_top_n, now=NOW)
        assert len(top_10) == 10

        signals_by_symbol = {signal.symbol: signal for signal in top_40}
        events_by_symbol = {event.symbol: event for event in events}
        llm_inputs = [
            LLMAnalysisInput(
                symbol=candidate.symbol,
                technical_signal=signals_by_symbol[candidate.symbol],
                events=[events_by_symbol[candidate.symbol]],
                portfolio_exposure_pct=0,
            )
            for candidate in top_10
        ]
        mock_llm = MockLLMProvider(
            [
                LLMCompletion(
                    content=_analysis_json(item.symbol, item.events[0].id),
                    model_name="offline-mock-v1",
                    input_tokens=100,
                    output_tokens=80,
                    total_tokens=180,
                )
                for item in llm_inputs
            ]
        )
        analyses = LLMAnalyst(
            mock_llm,
            LLMAnalysisRepository(database),
            model_name="offline-mock-v1",
            max_candidates=settings.analysis_top_n,
        ).analyze(llm_inputs)
        assert len(analyses) == len(mock_llm.calls) == 10

        # Final scoring is deterministic and uses configured weights. The LLM
        # component is evidence-bounded: confidence scales catalyst strength.
        ranking_by_symbol = {item.symbol: item for item in top_10}
        scored = []
        score_weights = {
            key: value
            for key, value in settings.scoring.model_dump().items()
            if key not in {"buy_threshold", "sell_threshold"}
        }
        for item, analysis in zip(llm_inputs, analyses):
            technical = item.technical_signal
            llm_score = analysis.catalyst_score * analysis.confidence / 100
            final_score = weighted_score(
                {
                    "technical": technical.technical_score,
                    "momentum": technical.momentum_score,
                    "volume": technical.volume_score,
                    "news_kap": ranking_by_symbol[item.symbol].event_score,
                    "llm": llm_score,
                },
                score_weights,
            )
            scored.append((final_score, item, analysis))
        final_score, selected, analysis = max(scored, key=lambda row: (row[0], row[1].symbol))
        assert final_score >= settings.scoring.buy_threshold
        assert analysis.action_bias is Action.BUY

        # Risk is the final authority; the analyst cannot choose quantity.
        price = Decimal(str(selected.technical_signal.metrics["ema_9"]))
        trade_signal = TradeSignal(
            timestamp=NOW,
            symbol=selected.symbol,
            action=Action.BUY,
            score=final_score,
            reason="deterministic integrated score above configured threshold",
            strategy_version=settings.strategy_version,
            requested_price=float(price),
        )
        portfolio = PortfolioService(Decimal(str(settings.capital))).snapshot({}, NOW)
        risk_engine = DeterministicRiskEngine(
            settings.risk,
            RiskDecisionRepository(database),
            GlobalKillSwitch(SystemStateRepository(database)),
        )
        risk_decision = risk_engine.evaluate(
            RiskOrderRequest(
                signal_id=trade_signal.id,
                symbol=trade_signal.symbol,
                action=Action.BUY,
                entry_price=price,
                stop_price=price * Decimal("0.96"),
                price_timestamp=NOW,
                requested_quantity=10,
            ),
            portfolio,
            NOW,
        )
        assert risk_decision.approved and risk_decision.approved_quantity == 10

        broker = PaperBroker(
            settings.capital,
            settings.execution.commission_pct,
            settings.execution.slippage_pct,
            database=database,
            risk_settings=settings.risk,
            notifier=SafeNotificationDispatcher([recorder]),
        )
        order = broker.buy(trade_signal, risk_decision)
        assert order.side is Action.BUY
        assert broker.get_positions()[selected.symbol].quantity == 10

        # This models the mandatory beginning of the next cycle: exits run before
        # any new scan. A hard stop closes the position and emits notifications.
        stop_price = order.fill_price * Decimal("0.95")
        exits = broker.run_exit_checks(
            {selected.symbol: stop_price}, now=NOW + timedelta(minutes=1)
        )
        assert len(exits) == 1 and exits[0].reason == "STOP LOSS"
        assert broker.get_positions() == {}
        assert [event for event, _ in recorder.events] == ["BUY", "SELL", "STOP LOSS"]

        # Every durable pipeline boundary wrote its audit record.
        expected_counts = {
            "technical_signals": 40,
            "event_items": 10,
            "intelligence_rankings": 10,
            "llm_analysis_cache": 10,
            "risk_decision_records": 2,
            "paper_orders": 2,
            "paper_fills": 2,
        }
        for table, expected in expected_counts.items():
            count = database.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
            assert count == expected
    finally:
        database.close()
