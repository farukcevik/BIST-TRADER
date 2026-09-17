from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class FundamentalProviderStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class ValuationStatus(StrEnum):
    CHEAP = "CHEAP"
    FAIR = "FAIR"
    EXPENSIVE = "EXPENSIVE"
    EXTREME = "EXTREME"
    UNKNOWN = "UNKNOWN"


class RedFlagSeverity(StrEnum):
    INFORMATIONAL = "INFORMATIONAL"
    CAUTION = "CAUTION"
    BLOCKING = "BLOCKING"


class Metric(BaseModel):
    """A value with explicit availability; missing values are never scored as zero."""
    value: float | None = None
    available: bool = False
    source: str | None = None

    @model_validator(mode="after")
    def availability_matches_value(self) -> "Metric":
        if self.available != (self.value is not None):
            raise ValueError("available must be true exactly when value is present")
        return self


class FinancialPeriod(BaseModel):
    period_end: datetime
    period_start: datetime | None = None
    currency: str | None = None
    unit: float | None = None
    consolidated: bool | None = None
    filing_id: str | None = None
    published_at: datetime | None = None
    revenue: Metric = Field(default_factory=Metric)
    gross_profit: Metric = Field(default_factory=Metric)
    operating_profit: Metric = Field(default_factory=Metric)
    ebitda: Metric = Field(default_factory=Metric)
    net_income: Metric = Field(default_factory=Metric)
    net_interest_income: Metric = Field(default_factory=Metric)
    loans: Metric = Field(default_factory=Metric)
    deposits: Metric = Field(default_factory=Metric)
    operating_cash_flow: Metric = Field(default_factory=Metric)
    investing_cash_flow: Metric = Field(default_factory=Metric)
    capex: Metric = Field(default_factory=Metric)
    free_cash_flow: Metric = Field(default_factory=Metric)
    total_debt: Metric = Field(default_factory=Metric)
    short_term_financial_debt: Metric = Field(default_factory=Metric)
    long_term_financial_debt: Metric = Field(default_factory=Metric)
    cash_and_equivalents: Metric = Field(default_factory=Metric)
    net_debt: Metric = Field(default_factory=Metric)
    equity: Metric = Field(default_factory=Metric)
    total_assets: Metric = Field(default_factory=Metric)
    total_liabilities: Metric = Field(default_factory=Metric)
    outstanding_shares: Metric = Field(default_factory=Metric)
    market_cap: Metric = Field(default_factory=Metric)
    pe: Metric = Field(default_factory=Metric)
    pb: Metric = Field(default_factory=Metric)
    ev_ebitda: Metric = Field(default_factory=Metric)
    enterprise_value: Metric = Field(default_factory=Metric)
    sector_pe: Metric = Field(default_factory=Metric)
    sector_pb: Metric = Field(default_factory=Metric)
    sector_ev_ebitda: Metric = Field(default_factory=Metric)
    historical_pe_median: Metric = Field(default_factory=Metric)
    historical_pb_median: Metric = Field(default_factory=Metric)
    historical_ev_ebitda_median: Metric = Field(default_factory=Metric)
    gross_margin: Metric = Field(default_factory=Metric)
    operating_margin: Metric = Field(default_factory=Metric)
    ebitda_margin: Metric = Field(default_factory=Metric)
    net_margin: Metric = Field(default_factory=Metric)


class FundamentalSnapshot(BaseModel):
    symbol: str
    as_of: datetime
    source: str
    provider_status: FundamentalProviderStatus
    periods: list[FinancialPeriod] = Field(default_factory=list)
    audit_metadata: dict[str, object] = Field(default_factory=dict)


class RedFlag(BaseModel):
    code: str
    severity: RedFlagSeverity
    explanation: str


class FundamentalAssessment(BaseModel):
    symbol: str
    as_of: datetime
    provider_status: FundamentalProviderStatus
    fundamental_score: float | None = Field(default=None, ge=0, le=100)
    effective_score: float | None = Field(default=None, ge=0, le=100)
    coverage: float = Field(default=0, ge=0, le=100)
    confidence: float = Field(ge=0, le=100)
    valuation_status: ValuationStatus = ValuationStatus.UNKNOWN
    valuation_confidence: float = Field(default=0, ge=0, le=100)
    available_metrics: list[str] = Field(default_factory=list)
    missing_metrics: list[str] = Field(default_factory=list)
    red_flags: list[RedFlag] = Field(default_factory=list)
    positive_factors: list[str] = Field(default_factory=list)
    score_breakdown: dict[str, object] = Field(default_factory=dict)
    derived_metrics: dict[str, Metric] = Field(default_factory=dict)
    blocks_entry: bool = False
