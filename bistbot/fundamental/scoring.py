from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class FundamentalScoringSettings(BaseModel):
    """Conservative defaults emphasize cash-backed quality over headline growth."""
    weights: dict[str, float] = Field(default_factory=lambda: {
        "growth_score": .20, "profitability_score": .18,
        "cash_flow_quality_score": .20, "balance_sheet_score": .20,
        "valuation_score": .12, "consistency_score": .10,
    })
    minimum_confidence: float = Field(default=40, ge=0, le=100)
    blocking_red_flags: set[str] = Field(default_factory=lambda: {"NEGATIVE_EQUITY"})

    @model_validator(mode="after")
    def validate_weights(self) -> "FundamentalScoringSettings":
        if set(self.weights) != {"growth_score", "profitability_score", "cash_flow_quality_score",
                "balance_sheet_score", "valuation_score", "consistency_score"}:
            raise ValueError("fundamental weights must cover all score components")
        if any(weight < 0 for weight in self.weights.values()) or abs(sum(self.weights.values()) - 1) > 1e-9:
            raise ValueError("fundamental weights must be non-negative and sum to one")
        return self


def renormalized_score(values: dict[str, float | None], weights: dict[str, float]) -> tuple[float | None, dict[str, float]]:
    available = {name: value for name, value in values.items() if value is not None}
    total = sum(weights[name] for name in available)
    if not available or total <= 0:
        return None, {}
    normalized = {name: weights[name] / total for name in available}
    return sum(available[name] * normalized[name] for name in available), normalized


def band(value: float, bad: float, good: float, higher_is_better: bool = True) -> float:
    if good == bad:
        return 50
    score = (value - bad) / (good - bad) * 100
    if not higher_is_better:
        score = 100 - score
    return max(0, min(100, score))
