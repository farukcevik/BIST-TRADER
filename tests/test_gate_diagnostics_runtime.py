from __future__ import annotations

from datetime import datetime,timezone

from bistbot.app.config import load_settings
from bistbot.app.models import Action
from bistbot.app.runtime import BistBotApplication,_gate_rejection_counts
from bistbot.broker.paper import PaperBroker
from bistbot.fundamental import UnavailableFundamentalProvider
from bistbot.intelligence.kap_provider import MockKapProvider
from bistbot.intelligence.news_provider import MockNewsProvider
from bistbot.market.provider import DemoMarketDataProvider
from bistbot.market_regime.provider import StaticMacroNewsProvider
from bistbot.notifications.base import SafeNotificationDispatcher
from bistbot.storage.database import Database


NOW=datetime(2026,8,21,12,tzinfo=timezone.utc)


def application(tmp_path,name="runtime-gates.db"):
    settings=load_settings("config.v1.yaml")
    database=Database(str(tmp_path/name))
    broker=PaperBroker(settings.capital,database=database,risk_settings=settings.risk)
    market=DemoMarketDataProvider(now=lambda:NOW,requests_per_second=1_000_000,
        batch_size=523,sleeper=lambda _:None)
    app=BistBotApplication(settings,database,broker,SafeNotificationDispatcher(),market=market,
        news=MockNewsProvider(),kap=MockKapProvider(),macro_provider=StaticMacroNewsProvider(),
        fundamental_provider=UnavailableFundamentalProvider())
    return app,broker,database


def test_runtime_attaches_both_tiers_to_every_top_ten_and_aggregates_exact_counts(tmp_path):
    app,_,_=application(tmp_path)
    app.run_cycle(now=NOW,dry_run=True)
    candidates=app.last_diagnostics["candidates"]
    assert len(candidates)==app.settings.analysis_top_n==10
    assert all({"NORMAL","FLEX"} <= item["gate_diagnostics"].keys() for item in candidates)
    assert app.last_diagnostics["gate_rejection_counts"]==_gate_rejection_counts(candidates)


def test_runtime_recomputes_gate_diagnostics_after_fresh_quote_revalidation(tmp_path,monkeypatch):
    app,_,_=application(tmp_path,"revalidation.db")
    calls={}
    original=app._investment_gate_diagnostics
    def record(technical,fundamental,potential):
        calls.setdefault(technical.symbol,[]).append(None if potential is None else potential.entry_rr)
        return original(technical,fundamental,potential)
    monkeypatch.setattr(app,"_investment_gate_diagnostics",record)
    original_decision=app._investment_decision
    decision_calls={}
    def force_revalidation(technical,fundamental,catalyst,potential):
        decision=original_decision(technical,fundamental,catalyst,potential)
        decision_calls[technical.symbol]=decision_calls.get(technical.symbol,0)+1
        if decision_calls[technical.symbol]>1:
            return decision
        return decision.model_copy(update={"action":Action.BUY,"buy_tier":"NORMAL","position_size_multiplier":1})
    monkeypatch.setattr(app,"_investment_decision",force_revalidation)

    app.run_cycle(now=NOW,dry_run=True)

    refreshed=[item for item in app.last_diagnostics["candidates"] if len(calls[item["symbol"]])>=2]
    assert refreshed
    for item in refreshed:
        assert item["gate_diagnostics"]["NORMAL"]["rr"]["actual"]==calls[item["symbol"]][-1]
    assert app.last_diagnostics["gate_rejection_counts"]==_gate_rejection_counts(
        app.last_diagnostics["candidates"])


def test_runtime_diagnostic_failure_is_fail_open_and_does_not_mutate_trading_state(tmp_path,monkeypatch):
    app,broker,database=application(tmp_path,"fail-open.db")
    cash_before=broker.get_cash(); positions_before=broker.get_positions()
    def broken(*args,**kwargs): raise RuntimeError("diagnostic fixture")
    monkeypatch.setattr(app.strategy,"diagnose_investment_gates",broken)

    summary=app.run_cycle(now=NOW,dry_run=True)

    assert summary["llm_candidates"]==10
    assert all(item["gate_diagnostics"]["status"]=="UNAVAILABLE" and
        item["gate_diagnostics"]["error"]=="RuntimeError" for item in app.last_diagnostics["candidates"])
    assert broker.get_cash()==cash_before and broker.get_positions()==positions_before
    for table in ("fundamental_snapshots","fundamental_scores","technical_levels",
                  "potential_assessments","risk_decisions","paper_orders","paper_fills"):
        assert database.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]==0
