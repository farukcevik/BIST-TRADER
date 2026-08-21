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


class LLMSettings(BaseModel):
    provider: str = "disabled"
    model: str = ""
    enabled: bool = False
    timeout_seconds: float = Field(default=30, gt=0)

    @model_validator(mode="after")
    def openai_requires_model(self) -> "LLMSettings":
        if self.enabled and self.provider.lower() == "openai" and not self.model.strip():
            raise ValueError("llm.model is required when the OpenAI provider is enabled")
        return self


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
    llm: LLMSettings = Field(default_factory=LLMSettings)


def load_settings(path: str | Path = "config.yaml", capital: float | None = None) -> Settings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if "system" in raw:
        raw = _normalize_alternate_config(raw)
    if capital is not None:
        raw["capital"] = capital
    return Settings.model_validate(raw)


def _normalize_alternate_config(source: dict) -> dict:
    """Accept the early nested config layout while preserving V1 safety ceilings."""
    portfolio=source.get("portfolio",{}); risk=source.get("risk",{}); exits=source.get("exit",{})
    scanner=source.get("scanner",{}); scanner_weights=dict(scanner.get("weights",{}))
    scanner_weights["volatility"]=scanner_weights.pop("trend",scanner_weights.get("volatility",.10))
    intelligence=source.get("intelligence",{}); final_score=source.get("final_score",{})
    strategy=source.get("strategy",{}); paper=source.get("paper",{}); scheduler=source.get("scheduler",{})
    return {"capital":portfolio.get("initial_capital",200000),"database":source.get("database","bistbot.db"),
        "strategy_version":source.get("system",{}).get("strategy_version","v1.0.0"),
        "schedule_seconds":scheduler.get("market_scan_seconds",300),
        "scanner_top_n":min(40,scanner.get("candidate_limit",40)),
        "analysis_top_n":min(10,intelligence.get("max_llm_calls_per_cycle",10)),
        "scanner":{"weights":scanner_weights,"minimum_bars":scanner.get("minimum_bars",22),
            "recent_high_lookback":scanner.get("recent_high_lookback",20),
            "relative_volume_lookback":scanner.get("relative_volume_lookback",20),
            "max_data_age_minutes":scanner.get("max_data_age_minutes",30),
            "minimum_average_volume":scanner.get("minimum_average_volume",100000),
            "minimum_average_turnover_try":scanner.get("minimum_average_turnover_try",1000000),
            "target_volatility_pct":scanner.get("target_volatility_pct",1.5),
            "maximum_preferred_volatility_pct":scanner.get("maximum_preferred_volatility_pct",5.0)},
        "intelligence":{"kap_trust_score":intelligence.get("kap_trust_score",95),
            "news_trust_score":intelligence.get("news_trust_score",65),
            "recency_half_life_hours":intelligence.get("recency_half_life_hours",24),
            "scanner_weight":intelligence.get("scanner_weight",.4),"event_weight":intelligence.get("event_weight",.6),
            "event_score_weights":intelligence.get("event_score_weights",{"recency":.25,"source_trust":.25,
                "symbol_relevance":.2,"keywords":.15,"materiality":.15})},
        "risk":{"max_open_positions":portfolio.get("max_open_positions",5),
            "max_position_pct":portfolio.get("max_position_pct",.15),"min_cash_pct":portfolio.get("min_cash_pct",.15),
            "max_trade_risk_pct":risk.get("max_trade_risk_pct",.005),"max_daily_loss_pct":risk.get("max_daily_loss_pct",.02),
            "max_weekly_loss_pct":risk.get("max_weekly_loss_pct",.05),"max_total_drawdown_pct":risk.get("max_total_drawdown_pct",.10),
            "default_stop_loss_pct":exits.get("default_stop_loss_pct",.04),
            "default_take_profit_pct":exits.get("default_take_profit_pct",.08),
            "default_trailing_stop_pct":exits.get("trailing_stop_pct",.035),
            "max_holding_days":exits.get("max_holding_days",20),"max_price_age_minutes":risk.get("max_price_age_minutes",5)},
        "execution":{"commission_pct":paper.get("commission_pct",.001),"slippage_pct":paper.get("slippage_pct",.0005)},
        "llm":source.get("llm",{}),
        "scoring":{**{key:final_score.get(key,value) for key,value in {"technical":.3,"momentum":.2,"volume":.15,
            "news_kap":.2,"llm":.15}.items()},"buy_threshold":strategy.get("buy_threshold",65),
            "sell_threshold":strategy.get("sell_threshold",35)}}
