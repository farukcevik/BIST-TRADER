from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from bistbot.app.models import IntelligenceStatus, LLMAnalysis, LLMStatus, RankedEventCandidate


class CatalystAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    NO_MATERIAL_EVENTS = "NO_MATERIAL_EVENTS"
    PROVIDERS_UNAVAILABLE = "PROVIDERS_UNAVAILABLE"


class CatalystAssessment(BaseModel):
    """A distinct, explainable "why now" dimension; never a price target."""

    catalyst_score: float | None = Field(default=None, ge=0, le=100)
    confidence: float = Field(ge=0, le=100)
    availability: CatalystAvailability
    event_score: float | None = Field(default=None, ge=0, le=100)
    llm_catalyst_score: float | None = Field(default=None, ge=0, le=100)
    components: dict[str, float] = Field(default_factory=dict)
    positive_factors: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)


def assess_catalyst(
    ranking: RankedEventCandidate,
    analysis: LLMAnalysis,
    *,
    providers_available: bool,
    event_weight: float = 0.65,
    llm_weight: float = 0.35,
) -> CatalystAssessment:
    """Score only available catalyst evidence with conservative renormalization.

    The LLM component is confidence- and priced-in-adjusted. It cannot create a
    catalyst when deterministic event materiality found no eligible event.
    """
    if event_weight < 0 or llm_weight < 0 or event_weight + llm_weight <= 0:
        raise ValueError("catalyst weights must be non-negative with a positive total")

    if ranking.intelligence_status is not IntelligenceStatus.EVENTS_AVAILABLE:
        availability = (CatalystAvailability.NO_MATERIAL_EVENTS if providers_available
                        else CatalystAvailability.PROVIDERS_UNAVAILABLE)
        caution = ("No material news/KAP catalyst was found" if providers_available
                   else "News/KAP providers unavailable; catalyst is unknown, not zero")
        return CatalystAssessment(availability=availability, confidence=0, cautions=[caution])

    values = {"event": float(ranking.event_score)}
    weights = {"event": event_weight}
    llm_value = None
    if analysis.llm_status is LLMStatus.AVAILABLE:
        confidence_factor = analysis.confidence / 100
        not_priced_in_factor = 1 - analysis.priced_in_probability / 100
        llm_value = analysis.catalyst_score * confidence_factor * not_priced_in_factor
        values["llm"] = llm_value
        weights["llm"] = llm_weight

    total_weight = sum(weights.values())
    normalized = {name: weight / total_weight for name, weight in weights.items()}
    score = sum(values[name] * normalized[name] for name in values)
    # Confidence reflects deterministic event evidence plus bounded LLM
    # corroboration; provider absence never masquerades as high confidence.
    confidence = 60.0
    if llm_value is not None:
        confidence = min(100.0, 60.0 + analysis.confidence * 0.4)
    factors = ["Material news/KAP evidence is available"]
    if llm_value is not None and llm_value >= 35:
        factors.append("LLM catalyst context corroborates the event evidence")
    cautions = []
    if analysis.llm_status is not LLMStatus.AVAILABLE:
        cautions.append("LLM catalyst context unavailable; score uses deterministic event evidence only")
    elif analysis.priced_in_probability >= 70:
        cautions.append("Catalyst may already be substantially priced in")
    return CatalystAssessment(catalyst_score=round(score, 4), confidence=round(confidence, 2),
        availability=CatalystAvailability.AVAILABLE, event_score=ranking.event_score,
        llm_catalyst_score=None if llm_value is None else round(llm_value, 4),
        components={name: round(values[name] * normalized[name], 4) for name in values},
        positive_factors=factors, cautions=cautions)
