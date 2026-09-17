from __future__ import annotations

from bistbot.app.models import Action, IntelligenceStatus, LLMAnalysis, LLMStatus, RankedEventCandidate
from bistbot.intelligence.catalyst import CatalystAvailability, assess_catalyst


def ranking(*, events: bool, score: float = 80) -> RankedEventCandidate:
    return RankedEventCandidate(symbol="AAA.IS", scanner_score=70, event_score=score,
        combined_score=70, intelligence_status=(IntelligenceStatus.EVENTS_AVAILABLE if events
                                                  else IntelligenceStatus.NO_NEWS))


def analysis(*, status: LLMStatus = LLMStatus.AVAILABLE, confidence: int = 80,
             catalyst: int = 90, priced_in: int = 25) -> LLMAnalysis:
    return LLMAnalysis(symbol="AAA.IS", sentiment=20, importance=70, catalyst_score=catalyst,
        priced_in_probability=priced_in, risk_score=20, confidence=confidence, time_horizon="swing",
        action_bias=Action.BUY, summary="bounded context", bull_case="", bear_case="",
        llm_status=status)


def test_unavailable_providers_are_unknown_not_zero():
    result = assess_catalyst(ranking(events=False), analysis(status=LLMStatus.NOT_REQUIRED),
                             providers_available=False)
    assert result.availability is CatalystAvailability.PROVIDERS_UNAVAILABLE
    assert result.catalyst_score is None
    assert "unknown, not zero" in result.cautions[0]


def test_available_provider_without_material_events_is_explicit():
    result = assess_catalyst(ranking(events=False), analysis(status=LLMStatus.NOT_REQUIRED),
                             providers_available=True)
    assert result.availability is CatalystAvailability.NO_MATERIAL_EVENTS
    assert result.catalyst_score is None


def test_llm_context_is_confidence_and_priced_in_adjusted():
    result = assess_catalyst(ranking(events=True), analysis(), providers_available=True)
    # LLM evidence is 90 * .80 * .75 = 54 before weighted combination.
    assert result.llm_catalyst_score == 54
    assert result.catalyst_score == 70.9
    assert result.components == {"event": 52.0, "llm": 18.9}


def test_missing_llm_renormalizes_to_event_evidence():
    result = assess_catalyst(ranking(events=True, score=73),
        analysis(status=LLMStatus.UNAVAILABLE), providers_available=True)
    assert result.catalyst_score == 73
    assert result.components == {"event": 73}
    assert result.confidence == 60
