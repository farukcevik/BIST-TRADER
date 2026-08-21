from __future__ import annotations

from datetime import datetime
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


class IntelligenceStatus(StrEnum):
    NO_NEWS = "NO_NEWS"
    EVENTS_AVAILABLE = "EVENTS_AVAILABLE"


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


class RiskOutcome(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REDUCE_SIZE = "REDUCE_SIZE"
    HALT_TRADING = "HALT_TRADING"


class RiskReasonCode(StrEnum):
    APPROVED = "APPROVED"
    KILL_SWITCH = "KILL_SWITCH"
    MAX_POSITIONS = "MAX_POSITIONS"
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
