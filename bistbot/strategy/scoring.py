from collections.abc import Mapping


def weighted_score(components: Mapping[str, float], weights: Mapping[str, float]) -> float:
    if set(components) != set(weights): raise ValueError("components and weights must match")
    if abs(sum(weights.values()) - 1.0) > 1e-9: raise ValueError("weights must sum to 1.0")
    return sum(components[key] * weights[key] for key in weights)

