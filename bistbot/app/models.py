from __future__ import annotations

from datetime import date,datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Action(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class MarketSnapshot(BaseModel):
    symbol: str
    timestamp: datetime
    price: float = Field(gt=0)
    volume: float = Field(ge=0)
    fields: dict[str, float] = Field(default_factory=dict)
    is_stale: bool = False

class QuoteFreshnessStatus(StrEnum):
    FRESH="FRESH"
    CURRENT_SESSION_DELAYED_ACCEPTED="CURRENT_SESSION_DELAYED_ACCEPTED"
    STALE_CURRENT_SESSION="STALE_CURRENT_SESSION"
    PREVIOUS_SESSION="PREVIOUS_SESSION"
    NO_TIMESTAMP="NO_TIMESTAMP"

class ExecutionQuote(BaseModel):
    symbol: str
    price: float = Field(gt=0)
    source_timestamp: datetime|None
    fetched_at: datetime
    provider: str
    quote_age_seconds: float|None = Field(default=None,ge=0)
    freshness_limit_seconds: float = Field(gt=0)
    session_date: date|None = None
    freshness_status: QuoteFreshnessStatus

class ExecutionQuoteValidation(BaseModel):
    valid: bool
    provider: str
    price: Decimal|None = None
    source_timestamp: datetime|None
    evaluated_at: datetime
    session_date: date|None = None
    quote_age_seconds: float|None = Field(default=None,ge=0)
    freshness_limit_seconds: float = Field(gt=0)
    freshness_status: QuoteFreshnessStatus
    reason: str


class Candle(BaseModel):
    """A completed OHLCV bar used by deterministic market calculations."""
    symbol: str
    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)


# Backward-compatible name consumed by the deterministic scanner.
MarketBar = Candle


class ProviderDiagnostics(BaseModel):
    symbol: str
    request_timestamp: datetime
    data_timestamp: datetime | None = None
    latency_ms: float = Field(ge=0)
    is_stale: bool
    error_reason: str | None = None
    attempts: int = Field(ge=1)


class MarketDataResult(BaseModel):
    symbol: str
    candles: list[Candle] = Field(default_factory=list)
    snapshot: MarketSnapshot | None = None
    diagnostics: ProviderDiagnostics

    @property
    def available(self) -> bool:
        return self.snapshot is not None and not self.diagnostics.is_stale and self.diagnostics.error_reason is None


class TechnicalSignal(BaseModel):
    symbol: str
    timestamp: datetime
    technical_score: float = Field(ge=0, le=100)
    momentum_score: float = Field(ge=0, le=100)
    volume_score: float = Field(ge=0, le=100)
    trend_score: float = Field(default=50,ge=0,le=100)
    liquidity_score: float = Field(ge=0, le=100)
    volatility_score: float = Field(ge=0, le=100)
    overall_scanner_score: float = Field(ge=0, le=100)
    metrics: dict[str, float | bool | str]
    reasons: list[str] = Field(default_factory=list)


class EventSourceType(StrEnum):
    NEWS = "NEWS"
    KAP = "KAP"


class MarketRegime(StrEnum):
    RISK_ON = "RISK_ON"
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    RISK_OFF = "RISK_OFF"
    CRISIS = "CRISIS"


class MacroRiskCategory(StrEnum):
    TURKEY_MACRO = "turkey_macro_risk"
    DOMESTIC_POLICY = "domestic_policy_risk"
    GEOPOLITICAL = "geopolitical_risk"
    GLOBAL_MARKET = "global_market_risk"
    FX = "fx_risk"
    COMMODITY = "commodity_risk"
    SYSTEMIC_EVENT = "systemic_event_risk"


class TimestampSource(StrEnum):
    SOURCE = "SOURCE"
    PARSED_PAGE = "PARSED_PAGE"
    FETCH_FALLBACK = "FETCH_FALLBACK"


class MacroEvent(BaseModel):
    canonical_event_id: str
    source_id: str
    source: str
    title: str
    body: str = ""
    url: str | None = None
    published_at: datetime
    fetched_at: datetime
    timestamp_source: TimestampSource = TimestampSource.SOURCE
    category: MacroRiskCategory
    source_reliability: float = Field(ge=0, le=100)
    materiality_score: float = Field(default=0, ge=0, le=100)
    materiality: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"
    event_type: str = "ROUTINE"
    age_hours: float = Field(default=0,ge=0)
    freshness_weight: float = Field(default=1,ge=0,le=1)
    effective_materiality: float = Field(default=0,ge=0,le=100)
    confirmation_score: float = Field(default=100,ge=0,le=100)
    regime_contribution: float = Field(default=0,ge=0,le=100)
    direction: Literal["POSITIVE", "NEUTRAL", "NEGATIVE", "MIXED"] = "NEUTRAL"
    affected_sectors: list[str] = Field(default_factory=list)
    positive_sectors: list[str] = Field(default_factory=list)
    negative_sectors: list[str] = Field(default_factory=list)


class MarketRegimeState(BaseModel):
    regime: MarketRegime = MarketRegime.NORMAL
    market_risk_score: int = Field(default=0, ge=0, le=100)
    confidence: int = Field(default=0, ge=0, le=100)
    risk_categories: dict[str, int] = Field(default_factory=dict)
    event_summary: str = "No material macro event"
    expected_market_direction: str = "NEUTRAL"
    expected_duration: str = "unknown"
    affected_sectors: list[str] = Field(default_factory=list)
    positive_sectors: list[str] = Field(default_factory=list)
    negative_sectors: list[str] = Field(default_factory=list)
    uncertainty: str = ""
    source_ids: list[str] = Field(default_factory=list)
    material_events: list[MacroEvent] = Field(default_factory=list)
    last_updated: datetime = Field(default_factory=datetime.now)


class MacroLLMAnalysis(BaseModel):
    regime: MarketRegime
    market_risk_score: int = Field(ge=0,le=100)
    confidence: int = Field(ge=0,le=100)
    event_summary: str
    expected_market_direction: str
    expected_duration: str
    affected_sectors: list[str] = Field(default_factory=list)
    positive_sectors: list[str] = Field(default_factory=list)
    negative_sectors: list[str] = Field(default_factory=list)
    uncertainty: str
    source_ids: list[str]


class MarketOverlay(BaseModel):
    market_regime: MarketRegime
    market_adjustment: float = 0
    sector_adjustment: float = 0
    buy_threshold_adjustment: float = 0
    position_multiplier: float = Field(default=1, ge=0, le=1)
    block_new_entries: bool = False


class IntelligenceStatus(StrEnum):
    NO_NEWS = "NO_NEWS"
    EVENTS_AVAILABLE = "EVENTS_AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class ProviderState(StrEnum):
    AVAILABLE_WITH_EVENTS = "AVAILABLE_WITH_EVENTS"
    AVAILABLE_NO_EVENTS = "AVAILABLE_NO_EVENTS"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


class IntelligenceProviderDiagnostics(BaseModel):
    provider: str
    mode: str
    endpoint: str | None = None
    request_timestamp: datetime | None = None
    http_status: int | None = None
    response_size: int = Field(default=0, ge=0)
    raw_events: int = Field(default=0, ge=0)
    parsed_events: int = Field(default=0, ge=0)
    mapped_events: int = Field(default=0, ge=0)
    symbols_matched: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    status: ProviderState = ProviderState.UNAVAILABLE
    latest_event: datetime | None = None
    error_type: str | None = None
    error_message: str | None = None
    cooldown_until: datetime | None = None
    cursor_published_at: datetime | None = None
    backfill_from: datetime | None = None
    cached_events: int = Field(default=0, ge=0)
    cleanup_deleted: int = Field(default=0, ge=0)


class LLMStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_REQUIRED = "NOT_REQUIRED"
    INVALID = "INVALID"


class SignalMode(StrEnum):
    TECHNICAL_ONLY = "TECHNICAL_ONLY"
    TECHNICAL_PLUS_NEWS = "TECHNICAL_PLUS_NEWS"


class EventItem(BaseModel):
    id: str
    symbol: str
    source: str
    source_type: EventSourceType
    title: str
    body: str = ""
    url: str | None = None
    published_at: datetime
    fetched_at: datetime
    hash: str
    trust_score: float = Field(ge=0, le=100)
    event_type: str = "UNCLASSIFIED"
    materiality_score: float = Field(default=0,ge=0,le=100)
    source_reliability: float = Field(default=0,ge=0,le=100)
    verification: str = "UNVERIFIED"
    materiality_reason: str = ""


class RankedEventCandidate(BaseModel):
    symbol: str
    scanner_score: float = Field(ge=0, le=100)
    event_score: float = Field(ge=0, le=100)
    combined_score: float = Field(ge=0, le=100)
    event_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    intelligence_status: IntelligenceStatus = IntelligenceStatus.NO_NEWS


class StrategyDecision(BaseModel):
    symbol: str
    action: Action
    final_score: float = Field(ge=0,le=100)
    reason: str
    signal_mode: SignalMode = SignalMode.TECHNICAL_ONLY
    score_breakdown: dict[str, Any] = Field(default_factory=dict)


class NewsItem(BaseModel):
    source_id: str
    source: str
    url: str | None = None
    timestamp: datetime
    title: str
    body: str = ""
    symbol: str
    relevance: float = Field(default=0, ge=0, le=100)
    content_hash: str


class LLMAnalysis(BaseModel):
    symbol: str
    sentiment: int = Field(ge=-100, le=100)
    importance: int = Field(ge=0, le=100)
    catalyst_score: int = Field(ge=0, le=100)
    priced_in_probability: int = Field(ge=0, le=100)
    risk_score: int = Field(ge=0, le=100)
    confidence: int = Field(ge=0, le=100)
    time_horizon: Literal["intraday", "swing", "none"]
    action_bias: Action
    summary: str
    bull_case: str
    bear_case: str
    risks: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    llm_status: LLMStatus = LLMStatus.AVAILABLE


class LLMAnalysisInput(BaseModel):
    symbol: str
    technical_signal: TechnicalSignal
    events: list[EventItem] = Field(default_factory=list)
    portfolio_exposure_pct: float | None = Field(default=None, ge=0, le=100)


class LLMCompletion(BaseModel):
    content: str
    model_name: str
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class TradeSignal(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=datetime.now)
    symbol: str
    action: Action
    score: float = Field(ge=0, le=100)
    reason: str
    strategy_version: str
    requested_price: float = Field(gt=0)
    buy_tier: Literal["NORMAL","FLEX"] | None = None


class RiskOutcome(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REDUCE_SIZE = "REDUCE_SIZE"
    HALT_TRADING = "HALT_TRADING"


class RiskReasonCode(StrEnum):
    APPROVED = "APPROVED"
    KILL_SWITCH = "KILL_SWITCH"
    MAX_OPEN_POSITIONS = "MAX_OPEN_POSITIONS"
    MAX_POSITIONS = "MAX_OPEN_POSITIONS"
    MAX_POSITION_SIZE = "MAX_POSITION_SIZE"
    MAX_TRADE_RISK = "MAX_TRADE_RISK"
    MIN_CASH_RESERVE = "MIN_CASH_RESERVE"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    WEEKLY_LOSS_LIMIT = "WEEKLY_LOSS_LIMIT"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INVALID_STOP = "INVALID_STOP"
    STALE_PRICE = "STALE_PRICE"
    INVALID_REQUEST = "INVALID_REQUEST"


class RiskOrderRequest(BaseModel):
    signal_id: UUID
    symbol: str
    action: Action
    entry_price: Decimal = Field(gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    price_timestamp: datetime
    execution_quote_validation: ExecutionQuoteValidation|None = None
    requested_quantity: int | None = Field(default=None, gt=0)


class RiskDecision(BaseModel):
    signal_id: UUID
    timestamp: datetime = Field(default_factory=datetime.now)
    outcome: RiskOutcome
    reason_code: RiskReasonCode
    reason: str
    requested_quantity: int | None = Field(default=None, ge=0)
    approved_quantity: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.outcome in {RiskOutcome.APPROVE, RiskOutcome.REDUCE_SIZE}

    @property
    def quantity(self) -> int:
        return self.approved_quantity


class Order(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    signal_id: UUID
    timestamp: datetime = Field(default_factory=datetime.now)
    symbol: str
    action: Action
    quantity: int = Field(gt=0)
    requested_price: float = Field(gt=0)
    fill_price: float | None = Field(default=None, gt=0)
    score: float = Field(ge=0, le=100)
    reason: str
    strategy_version: str
    status: OrderStatus = OrderStatus.PENDING


class ExitReason(StrEnum):
    HARD_STOP = "STOP LOSS"
    TAKE_PROFIT = "TAKE PROFIT"
    TRAILING_STOP = "TRAILING STOP"
    STRATEGY_EXIT = "STRATEGY EXIT"
    TIME_STOP = "TIME STOP"


class PaperOrder(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    signal_id: UUID
    symbol: str
    side: Action
    quantity: int = Field(gt=0)
    requested_price: Decimal = Field(gt=0)
    fill_price: Decimal | None = Field(default=None,gt=0)
    timestamp: datetime
    reason: str
    final_score: float = Field(ge=0,le=100)
    risk_decision: RiskDecision
    strategy_version: str
    status: OrderStatus
    buy_tier: Literal["NORMAL","FLEX"] | None = None


class PaperFill(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    order_id: UUID
    timestamp: datetime
    symbol: str
    side: Action
    quantity: int = Field(gt=0)
    fill_price: Decimal = Field(gt=0)
    commission: Decimal = Field(ge=0)
    realized_pnl: Decimal = Decimal("0")
    buy_tier: Literal["NORMAL","FLEX"] | None = None


class Position(BaseModel):
    symbol: str
    quantity: int = Field(gt=0)
    average_price: float = Field(gt=0)
    high_price: float = Field(gt=0)
    opened_at: datetime
    updated_at: datetime


class PortfolioSnapshot(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.now)
    cash: float = Field(ge=0)
    portfolio_value: float = Field(ge=0)
