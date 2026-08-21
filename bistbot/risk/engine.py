from __future__ import annotations

from datetime import datetime,timedelta,timezone
from decimal import Decimal

from bistbot.app.config import RiskSettings
from bistbot.app.models import Action,RiskDecision,RiskOrderRequest,RiskOutcome,RiskReasonCode
from bistbot.portfolio.service import PortfolioState
from bistbot.storage.repositories import RiskDecisionRepository,SystemStateRepository
from .position_sizing import DeterministicPositionSizer


class GlobalKillSwitch:
    def __init__(self,repository: SystemStateRepository): self.repository=repository
    @property
    def active(self) -> bool: return self.repository.get("global_kill_switch","false").lower()=="true"
    def activate(self) -> None: self.repository.set("global_kill_switch","true")
    def deactivate(self) -> None: self.repository.set("global_kill_switch","false")


class DeterministicRiskEngine:
    """Final deterministic authority. It consumes proposals; it never consumes LLM authority."""
    def __init__(self,settings: RiskSettings,repository: RiskDecisionRepository,kill_switch: GlobalKillSwitch):
        self.settings,self.repository,self.kill_switch=settings,repository,kill_switch
        self.sizer=DeterministicPositionSizer(settings)

    def evaluate(self,request: RiskOrderRequest,portfolio: PortfolioState,now: datetime | None=None) -> RiskDecision:
        now=now or datetime.now(timezone.utc)
        if request.action not in {Action.BUY,Action.SELL}:
            return self._save(request,RiskOutcome.REJECT,RiskReasonCode.INVALID_REQUEST,"Risk entry validation accepts BUY proposals only")
        if request.action is Action.BUY and self.kill_switch.active:
            return self._save(request,RiskOutcome.HALT_TRADING,RiskReasonCode.KILL_SWITCH,"Global kill switch is active")
        timestamp=request.price_timestamp if request.price_timestamp.tzinfo else request.price_timestamp.replace(tzinfo=timezone.utc)
        reference=now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        if (reference-timestamp).total_seconds()<0 or reference-timestamp > timedelta(minutes=self.settings.max_price_age_minutes):
            return self._save(request,RiskOutcome.REJECT,RiskReasonCode.STALE_PRICE,"Price is stale or future-dated")
        if request.action is Action.SELL:
            position=portfolio.positions.get(request.symbol)
            if position is None:
                return self._save(request,RiskOutcome.REJECT,RiskReasonCode.INVALID_REQUEST,"No position available to sell")
            requested=request.requested_quantity or position.quantity
            quantity=min(requested,position.quantity)
            outcome=RiskOutcome.REDUCE_SIZE if requested>position.quantity else RiskOutcome.APPROVE
            code=RiskReasonCode.MAX_POSITION_SIZE if outcome is RiskOutcome.REDUCE_SIZE else RiskReasonCode.APPROVED
            return self._save(request,outcome,code,"Position-reducing exit approved",quantity,
                              {"position_quantity":position.quantity})
        if request.stop_price is None or request.stop_price<=0 or request.stop_price>=request.entry_price:
            return self._save(request,RiskOutcome.REJECT,RiskReasonCode.INVALID_STOP,"Stop must be positive and below entry")
        if portfolio.daily_pnl <= -portfolio.initial_capital*Decimal(str(self.settings.max_daily_loss_pct)):
            return self._save(request,RiskOutcome.HALT_TRADING,RiskReasonCode.DAILY_LOSS_LIMIT,"Daily loss limit reached")
        if portfolio.weekly_pnl <= -portfolio.initial_capital*Decimal(str(self.settings.max_weekly_loss_pct)):
            return self._save(request,RiskOutcome.HALT_TRADING,RiskReasonCode.WEEKLY_LOSS_LIMIT,"Weekly loss limit reached")
        if portfolio.drawdown_pct >= Decimal(str(self.settings.max_total_drawdown_pct)):
            return self._save(request,RiskOutcome.HALT_TRADING,RiskReasonCode.MAX_DRAWDOWN,"Maximum drawdown reached")
        if request.symbol not in portfolio.positions and len(portfolio.positions)>=self.settings.max_open_positions:
            return self._save(request,RiskOutcome.REJECT,RiskReasonCode.MAX_POSITIONS,"Maximum open positions reached")
        sizing=self.sizer.size(request,portfolio); metadata={"maximum_by_position":sizing.maximum_by_position,
            "maximum_by_risk":sizing.maximum_by_risk,"maximum_by_cash":sizing.maximum_by_cash}
        if sizing.quantity<=0:
            code=RiskReasonCode.INSUFFICIENT_CASH if sizing.maximum_by_cash<=0 else sizing.limiting_reason
            return self._save(request,RiskOutcome.REJECT,code,"No risk-compliant quantity available",0,metadata)
        requested=request.requested_quantity
        if requested is not None and requested>sizing.quantity:
            return self._save(request,RiskOutcome.REDUCE_SIZE,sizing.limiting_reason,"Requested quantity reduced to risk maximum",sizing.quantity,metadata)
        approved=requested or sizing.quantity
        return self._save(request,RiskOutcome.APPROVE,RiskReasonCode.APPROVED,"Risk checks passed",approved,metadata)

    def _save(self,request: RiskOrderRequest,outcome: RiskOutcome,code: RiskReasonCode,reason: str,
              quantity: int=0,metadata: dict|None=None) -> RiskDecision:
        decision=RiskDecision(signal_id=request.signal_id,outcome=outcome,reason_code=code,reason=reason,
            requested_quantity=request.requested_quantity,approved_quantity=quantity,metadata=metadata or {})
        self.repository.add(decision); return decision
