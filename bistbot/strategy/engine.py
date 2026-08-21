from typing import Protocol
from bistbot.app.models import LLMAnalysis, MarketSnapshot, TradeSignal


class StrategyEngine(Protocol):
    def generate(self, market: MarketSnapshot, analysis: LLMAnalysis) -> TradeSignal: ...

