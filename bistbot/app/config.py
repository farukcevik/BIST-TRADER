from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class RiskSettings(BaseModel):
    max_open_positions: int = Field(gt=0)
    max_position_pct: float = Field(gt=0, le=1)
    min_cash_pct: float = Field(ge=0, lt=1)
    max_trade_risk_pct: float = Field(gt=0, le=1)
    max_daily_loss_pct: float = Field(gt=0, le=1)
    max_weekly_loss_pct: float = Field(gt=0, le=1)
    max_total_drawdown_pct: float = Field(gt=0, le=1)
    default_stop_loss_pct: float = Field(gt=0, lt=1)
    default_take_profit_pct: float = Field(gt=0)
    default_trailing_stop_pct: float = Field(gt=0, lt=1)
    max_holding_days: int = Field(gt=0)
    max_price_age_minutes: int = Field(gt=0)


class ExecutionSettings(BaseModel):
    commission_pct: float = Field(ge=0)
    slippage_pct: float = Field(ge=0)


class ScoringSettings(BaseModel):
    technical: float = Field(ge=0)
    momentum: float = Field(ge=0)
    volume: float = Field(ge=0)
    news_kap: float = Field(ge=0)
    llm: float = Field(ge=0)
    buy_threshold: float = Field(ge=0, le=100)
    sell_threshold: float = Field(ge=0, le=100)

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> "ScoringSettings":
        total = self.technical + self.momentum + self.volume + self.news_kap + self.llm
        if abs(total - 1.0) > 1e-9:
            raise ValueError("scoring weights must sum to 1.0")
        return self


class ScannerWeights(BaseModel):
    technical: float = Field(ge=0)
    momentum: float = Field(ge=0)
    volume: float = Field(ge=0)
    liquidity: float = Field(ge=0)
    volatility: float = Field(ge=0)

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> "ScannerWeights":
        if abs(self.technical + self.momentum + self.volume + self.liquidity + self.volatility - 1.0) > 1e-9:
            raise ValueError("scanner weights must sum to 1.0")
        return self


class ScannerSettings(BaseModel):
    weights: ScannerWeights
    minimum_bars: int = Field(default=22, ge=22)
    recent_high_lookback: int = Field(default=20, ge=2)
    relative_volume_lookback: int = Field(default=20, ge=2)
    max_data_age_minutes: int = Field(default=30, gt=0)
    minimum_average_volume: float = Field(gt=0)
    minimum_average_turnover_try: float = Field(gt=0)
    target_volatility_pct: float = Field(gt=0)
    maximum_preferred_volatility_pct: float = Field(gt=0)


class EventScoreWeights(BaseModel):
    recency: float = Field(ge=0)
    source_trust: float = Field(ge=0)
    symbol_relevance: float = Field(ge=0)
    keywords: float = Field(ge=0)
    materiality: float = Field(ge=0)

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> "EventScoreWeights":
        if abs(sum(self.model_dump().values()) - 1.0) > 1e-9:
            raise ValueError("event score weights must sum to 1.0")
        return self


class IntelligenceSettings(BaseModel):
    kap_trust_score: float = Field(ge=0, le=100)
    news_trust_score: float = Field(ge=0, le=100)
    recency_half_life_hours: float = Field(gt=0)
    scanner_weight: float = Field(ge=0, le=1)
    event_weight: float = Field(ge=0, le=1)
    event_score_weights: EventScoreWeights

    @model_validator(mode="after")
    def ranking_weights_sum_to_one(self) -> "IntelligenceSettings":
        if abs(self.scanner_weight + self.event_weight - 1.0) > 1e-9:
            raise ValueError("intelligence ranking weights must sum to 1.0")
        if self.kap_trust_score <= self.news_trust_score:
            raise ValueError("KAP trust must be higher than news trust")
        return self


class Settings(BaseModel):
    capital: float = Field(gt=0)
    database: str
    strategy_version: str
    schedule_seconds: int = Field(gt=0)
    scanner_top_n: int = Field(gt=0)
    analysis_top_n: int = Field(gt=0)
    risk: RiskSettings
    execution: ExecutionSettings
    scoring: ScoringSettings
    scanner: ScannerSettings
    intelligence: IntelligenceSettings


def load_settings(path: str | Path = "config.yaml", capital: float | None = None) -> Settings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if capital is not None:
        raw["capital"] = capital
    return Settings.model_validate(raw)
