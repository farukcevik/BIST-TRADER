from datetime import datetime, timezone
import ast
from pathlib import Path

import pytest

from bistbot.app import runtime as runtime_module
from bistbot.app.config import load_settings
from bistbot.app.models import Action
from bistbot.app.runtime import BistBotApplication
from bistbot.broker.paper import PaperBroker
from bistbot.fundamental import UnavailableFundamentalProvider
from bistbot.intelligence.kap_provider import MockKapProvider
from bistbot.intelligence.news_provider import MockNewsProvider
from bistbot.market.provider import DemoMarketDataProvider
from bistbot.market_regime.provider import StaticMacroNewsProvider
from bistbot.notifications.base import SafeNotificationDispatcher
from bistbot.storage.database import Database
from bistbot.strategy.levels import TechnicalLevels
from bistbot.strategy.potential import assess_potential


NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


def levels(**updates) -> TechnicalLevels:
    base = TechnicalLevels(
        symbol="AAA.IS",
        timestamp=NOW,
        support_1=97,
        resistance_1=104,
        resistance_2=108,
        confidence=80,
        swing_highs=[],
        swing_lows=[97],
    )
    return base.model_copy(update=updates)


def assess(item: TechnicalLevels, *, entry: float = 100, slippage: float = 0,
        zone_atr_fraction: float = .35, tick_size: float = .01):
    return assess_potential(
        entry, entry - 5, item, 2, 70, 70, 1.5, 80, 70, 50,
        paper_slippage_pct=slippage, zone_atr_fraction=zone_atr_fraction, tick_size=tick_size,
    )


def test_t1_and_t2_are_selected_from_independent_resistance_zones():
    result = assess(levels(
        resistance_1=104,
        resistance_2=104.5,
        strong_resistance_zones=[104.3],
        swing_highs=[104.6, 108],
    ))

    assert result.available
    assert result.target_1 == 104
    assert result.target_2 == 108
    assert result.target_2 - result.target_1 > 2 * .35


def test_targets_inside_entry_zone_are_rejected_as_meaningless():
    result = assess(levels(resistance_1=100.5, resistance_2=100.7, swing_highs=[]))

    assert not result.available
    assert result.unavailable_reason == "INSUFFICIENT_VALIDATED_TARGETS"


def test_rr_uses_paper_buy_execution_price_after_slippage():
    result = assess(levels(), slippage=.01)

    assert result.available
    assert result.target_components["paper_execution_price"] == 101
    assert result.downside_reference == pytest.approx(96.5)
    assert result.rr_t1 == pytest.approx((104 - 101) / (101 - 96.5), rel=1e-4)
    assert result.rr_t2 == pytest.approx((108 - 101) / (101 - 96.5), rel=1e-4)
    assert result.entry_rr == pytest.approx((result.rr_t1 + result.rr_t2) / 2, rel=1e-4)


def test_structural_stop_remains_capped_at_eight_percent_of_execution_price():
    accepted = assess(levels(support_1=93.5, swing_lows=[]), slippage=.01)
    rejected = assess(levels(support_1=93.4, swing_lows=[]), slippage=.01)

    assert accepted.available
    assert accepted.downside_risk_pct <= 8
    assert not rejected.available
    assert rejected.unavailable_reason == "REQUIRED_STOP_EXCEEDS_8_PERCENT"


def test_invalid_slippage_is_unavailable():
    result = assess(levels(), slippage=-.01)

    assert not result.available
    assert result.unavailable_reason == "INVALID_POTENTIAL_CALIBRATION"


def test_non_default_zone_settings_control_target_independence_and_meaningfulness():
    item = levels(resistance_1=101.1, resistance_2=102.1, swing_highs=[103.2])

    default = assess(item)
    configured = assess(item, zone_atr_fraction=.5, tick_size=1.25)

    assert default.available and default.target_1 == 101.1 and default.target_2 == 102.1
    assert not configured.available
    assert configured.unavailable_reason == "INSUFFICIENT_VALIDATED_TARGETS"


def test_paper_execution_price_matches_decimal_half_up_fill_at_non_round_boundary():
    result = assess(levels(resistance_1=104, resistance_2=108), entry=100.005, slippage=.0005)

    assert result.available
    assert result.target_components["paper_execution_price"] == 100.055
    risk = 100.055 - result.downside_reference
    assert result.rr_t1 == pytest.approx((104 - 100.055) / risk, rel=1e-4)


def test_entry_rr_is_not_rounded_up_across_unchanged_gate_boundary():
    # Raw average RR is 1.49996; a conventional four-place round would incorrectly
    # expose 1.5000 to the unchanged strategy threshold.
    item = levels(support_1=97, resistance_1=103, resistance_2=107.49972)
    result = assess(item)

    assert result.available
    assert result.entry_rr == 1.4999
    assert result.entry_rr < 1.5


def test_exposed_stop_is_conservative_and_all_math_uses_its_exact_value():
    result = assess(levels(support_1=93.42001, swing_lows=[]), entry=101)

    assert result.available
    assert result.downside_reference == 92.9201
    assert result.downside_risk_pct <= 8
    exact_risk = 101 - result.downside_reference
    assert result.rr_t1 == pytest.approx((104 - 101) / exact_risk, rel=1e-4)
    assert result.rr_t2 == pytest.approx((108 - 101) / exact_risk, rel=1e-4)


def test_all_runtime_potential_calls_forward_level_configuration():
    runtime_path = Path(__file__).parents[1] / "bistbot" / "app" / "runtime.py"
    tree = ast.parse(runtime_path.read_text())
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "assess_potential"]

    assert len(calls) == 3
    for call in calls:
        keywords = {item.arg: ast.unparse(item.value) for item in call.keywords}
        assert keywords["zone_atr_fraction"] == "self.settings.technical_levels.zone_atr_fraction"
        assert keywords["tick_size"] == "self.settings.technical_levels.tick_size"
    assert sum("paper_slippage_pct" in {item.arg for item in call.keywords} for call in calls) == 1


def test_runtime_spy_observes_non_default_level_configuration_on_analysis_and_cycle_paths(
        tmp_path, monkeypatch):
    runtime_now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
    settings = load_settings("config.v1.yaml")
    settings.technical_levels.zone_atr_fraction = .2
    settings.technical_levels.tick_size = .02
    database = Database(str(tmp_path / "potential-runtime.db"))
    broker = PaperBroker(settings.capital, database=database, risk_settings=settings.risk)
    market = DemoMarketDataProvider(now=lambda: runtime_now, requests_per_second=1_000_000,
        batch_size=523, sleeper=lambda _: None)
    app = BistBotApplication(settings, database, broker, SafeNotificationDispatcher(), market=market,
        news=MockNewsProvider(), kap=MockKapProvider(), macro_provider=StaticMacroNewsProvider(),
        fundamental_provider=UnavailableFundamentalProvider())
    observed = []
    original_assess = runtime_module.assess_potential

    def spy(*args, **kwargs):
        observed.append(kwargs.copy())
        result = original_assess(*args, **kwargs)
        if not result.available and "paper_slippage_pct" not in kwargs:
            adjusted = list(args)
            entry, atr_value = float(args[0]), float(args[3])
            adjusted[2] = args[2].model_copy(update={"support_1": entry - 2 * atr_value,
                "support_2": None, "strong_support_zones": [], "swing_lows": [],
                "resistance_1": entry + 4 * atr_value,
                "resistance_2": entry + 8 * atr_value,
                "strong_resistance_zones": [], "swing_highs": []})
            result = original_assess(*adjusted, **kwargs)
        return result

    monkeypatch.setattr(runtime_module, "assess_potential", spy)
    app.analyze_symbol("THYAO", now=runtime_now)
    assert observed
    assert all(call["zone_atr_fraction"] == .2 and call["tick_size"] == .02
               for call in observed)
    assert all("paper_slippage_pct" not in call for call in observed)

    observed.clear()
    original_decision = app._investment_decision

    def force_buy(*args, **kwargs):
        decision = original_decision(*args, **kwargs)
        if not args[-1].available:
            return decision
        return decision.model_copy(update={"action": Action.BUY, "buy_tier": "NORMAL",
            "position_size_multiplier": 1})

    monkeypatch.setattr(app, "_investment_decision", force_buy)
    app.run_cycle(now=runtime_now, dry_run=True)

    assert observed
    assert all(call["zone_atr_fraction"] == .2 and call["tick_size"] == .02
               for call in observed)
    assert any("paper_slippage_pct" not in call for call in observed)
    assert any(call.get("paper_slippage_pct") == settings.execution.slippage_pct
               for call in observed)
    database.close()
