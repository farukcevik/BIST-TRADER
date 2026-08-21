from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal,ROUND_FLOOR

from bistbot.app.config import RiskSettings
from bistbot.app.models import RiskOrderRequest,RiskReasonCode
from bistbot.portfolio.service import PortfolioState


@dataclass(frozen=True)
class SizingResult:
    quantity: int
    limiting_reason: RiskReasonCode
    maximum_by_position: int
    maximum_by_risk: int
    maximum_by_cash: int


class DeterministicPositionSizer:
    def __init__(self,settings: RiskSettings): self.settings=settings

    def size(self,request: RiskOrderRequest,portfolio: PortfolioState) -> SizingResult:
        assert request.stop_price is not None
        current = portfolio.positions.get(request.symbol)
        current_value = current.market_value if current else Decimal("0")
        allocation_left = max(Decimal("0"),portfolio.equity*Decimal(str(self.settings.max_position_pct))-current_value)
        cash_available = max(Decimal("0"),portfolio.cash-portfolio.equity*Decimal(str(self.settings.min_cash_pct)))
        loss_per_share = request.entry_price-request.stop_price
        risk_budget = portfolio.equity*Decimal(str(self.settings.max_trade_risk_pct))
        by_position = int((allocation_left/request.entry_price).to_integral_value(rounding=ROUND_FLOOR))
        by_risk = int((risk_budget/loss_per_share).to_integral_value(rounding=ROUND_FLOOR))
        by_cash = int((cash_available/request.entry_price).to_integral_value(rounding=ROUND_FLOOR))
        quantity = min(by_position,by_risk,by_cash)
        if quantity == by_cash: reason=RiskReasonCode.MIN_CASH_RESERVE if portfolio.cash>0 else RiskReasonCode.INSUFFICIENT_CASH
        elif quantity == by_position: reason=RiskReasonCode.MAX_POSITION_SIZE
        else: reason=RiskReasonCode.MAX_TRADE_RISK
        return SizingResult(max(0,quantity),reason,by_position,by_risk,by_cash)
