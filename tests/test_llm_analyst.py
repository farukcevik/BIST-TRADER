from __future__ import annotations

import json
from datetime import datetime,timezone

import pytest

from bistbot.app.models import Action, EventSourceType, LLMAnalysisInput, LLMCompletion, TechnicalSignal
from bistbot.intelligence.llm_provider import LLMAnalyst, MockLLMProvider
from bistbot.intelligence.ranking import make_event
from bistbot.storage.database import Database
from bistbot.storage.repositories import LLMAnalysisRepository


NOW = datetime(2026,8,21,12,0,tzinfo=timezone.utc)


def technical(symbol="AAA", score=70):
    return TechnicalSignal(symbol=symbol,timestamp=NOW,technical_score=score,momentum_score=65,volume_score=60,
        liquidity_score=80,volatility_score=55,overall_scanner_score=score,metrics={"rsi_14":58.123456},reasons=[])


def disclosure(symbol="AAA"):
    return make_event(symbol=symbol,source="KAP",source_type=EventSourceType.KAP,title="New facility disclosure",
        body="Company disclosed a capacity expansion.",published_at=NOW,fetched_at=NOW,trust_score=95)


def output(symbol="AAA",source_ids=None,action="HOLD",confidence=35):
    return json.dumps({"symbol":symbol,"sentiment":20,"importance":70,"catalyst_score":65,
        "priced_in_probability":40,"risk_score":35,"confidence":confidence,"time_horizon":"swing",
        "action_bias":action,"summary":"Evidence summary","bull_case":"Expansion may add capacity",
        "bear_case":"Execution risk","risks":["Execution risk"],"source_ids":source_ids or []})


@pytest.fixture
def repository(tmp_path):
    database = Database(str(tmp_path/"llm.db"))
    yield database,LLMAnalysisRepository(database)
    database.close()


def test_valid_structured_analysis_and_metadata_storage(repository):
    database,repo = repository; event = disclosure()
    mock = MockLLMProvider([LLMCompletion(content=output(source_ids=[event.id],action="BUY",confidence=72),
        model_name="mock-v1",input_tokens=120,output_tokens=80,total_tokens=200)])
    result = LLMAnalyst(mock,repo,model_name="mock-v1").analyze([LLMAnalysisInput(symbol="AAA",technical_signal=technical(),events=[event])])[0]
    assert result.action_bias is Action.BUY and result.source_ids == [event.id]
    row = database.query("SELECT * FROM llm_analysis_cache")[0]
    assert row["model_name"] == "mock-v1" and row["prompt_version"] == "llm-analysis-v3-material-evidence"
    assert row["input_tokens"] == 120 and row["total_tokens"] == 200 and row["latency_ms"] >= 0


def test_invalid_json_falls_back_once_to_hold(repository):
    _,repo = repository; mock = MockLLMProvider([LLMCompletion(content="not-json",model_name="mock-v1")])
    result = LLMAnalyst(mock,repo,model_name="mock-v1").analyze([LLMAnalysisInput(symbol="AAA",technical_signal=technical(),events=[disclosure()])])[0]
    assert result.action_bias is Action.HOLD and result.confidence <= 40
    assert len(mock.calls) == 1


def test_insufficient_evidence_skips_llm_and_holds(repository):
    _,repo = repository; mock = MockLLMProvider([])
    result = LLMAnalyst(mock,repo,model_name="mock-v1").analyze([LLMAnalysisInput(symbol="AAA",technical_signal=technical())])[0]
    assert result.action_bias is Action.HOLD and result.confidence == 0 and mock.calls == []


def test_fabricated_source_id_is_rejected(repository):
    _,repo = repository; mock = MockLLMProvider([LLMCompletion(content=output(source_ids=["invented"],action="BUY",confidence=90),model_name="mock-v1")])
    result = LLMAnalyst(mock,repo,model_name="mock-v1").analyze([LLMAnalysisInput(symbol="AAA",technical_signal=technical(),events=[disclosure()])])[0]
    assert result.action_bias is Action.HOLD and result.source_ids == []


def test_identical_meaningful_input_uses_cache(repository):
    _,repo = repository; event = disclosure()
    mock = MockLLMProvider([LLMCompletion(content=output(source_ids=[event.id]),model_name="mock-v1")])
    analyst = LLMAnalyst(mock,repo,model_name="mock-v1")
    candidate = LLMAnalysisInput(symbol="AAA",technical_signal=technical(),events=[event],portfolio_exposure_pct=5)
    assert analyst.analyze([candidate])[0] == analyst.analyze([candidate])[0]
    assert len(mock.calls) == 1


def test_market_state_change_triggers_new_call(repository):
    _,repo = repository; event = disclosure()
    completion = lambda:LLMCompletion(content=output(source_ids=[event.id]),model_name="mock-v1")
    mock = MockLLMProvider([completion(),completion()]); analyst = LLMAnalyst(mock,repo,model_name="mock-v1")
    analyst.analyze([LLMAnalysisInput(symbol="AAA",technical_signal=technical(score=70),events=[event])])
    analyst.analyze([LLMAnalysisInput(symbol="AAA",technical_signal=technical(score=71),events=[event])])
    assert len(mock.calls) == 2

def test_closed_market_event_driven_mode_reuses_event_cache_when_technical_state_changes(repository):
    _,repo=repository; event=disclosure()
    mock=MockLLMProvider([LLMCompletion(content=output(source_ids=[event.id]),model_name="mock-v1")])
    analyst=LLMAnalyst(mock,repo,model_name="mock-v1")
    analyst.analyze([LLMAnalysisInput(symbol="AAA",technical_signal=technical(score=70),events=[event])],event_driven=True)
    analyst.analyze([LLMAnalysisInput(symbol="AAA",technical_signal=technical(score=71),events=[event])],event_driven=True)
    assert len(mock.calls)==1 and analyst.cache_avoided_calls==1


def test_only_supplied_event_content_enters_prompt(repository):
    _,repo = repository; event = disclosure()
    mock = MockLLMProvider([LLMCompletion(content=output(source_ids=[event.id]),model_name="mock-v1")])
    LLMAnalyst(mock,repo,model_name="mock-v1").analyze([LLMAnalysisInput(symbol="AAA",technical_signal=technical(),events=[event])])
    prompt = json.loads(mock.calls[0]["input_json"])
    assert [item["id"] for item in prompt["events"]] == [event.id]
    assert prompt["events"][0]["body"] == event.body


def test_provider_error_falls_back_to_hold(repository):
    _,repo = repository; mock = MockLLMProvider([TimeoutError("down")])
    result = LLMAnalyst(mock,repo,model_name="mock-v1").analyze([LLMAnalysisInput(symbol="AAA",technical_signal=technical(),events=[disclosure()])])[0]
    assert result.action_bias is Action.HOLD and "unavailable" in result.summary


def test_candidate_limit_is_enforced(repository):
    _,repo = repository; analyst = LLMAnalyst(MockLLMProvider([]),repo,model_name="mock-v1")
    candidates = [LLMAnalysisInput(symbol=f"S{i}",technical_signal=technical(f"S{i}")) for i in range(11)]
    with pytest.raises(ValueError,match="candidate limit exceeded"): analyst.analyze(candidates)
