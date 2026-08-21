from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from pydantic import BaseModel, Field

CENT = Decimal("0.01")
ZERO = Decimal("0")


def money(value: Decimal | str | int | float) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


class PortfolioPosition(BaseModel):
    symbol: str
    quantity: int = Field(gt=0)
    average_price: Decimal = Field(gt=0)
    last_price: Decimal = Field(gt=0)
    opened_at: datetime
    updated_at: datetime

    @property
    def market_value(self) -> Decimal: return money(self.last_price*self.quantity)
    @property
    def unrealized_pnl(self) -> Decimal: return money((self.last_price-self.average_price)*self.quantity)


class PortfolioState(BaseModel):
    as_of: datetime
    initial_capital: Decimal = Field(gt=0)
    cash: Decimal = Field(ge=0)
    positions: dict[str,PortfolioPosition] = Field(default_factory=dict)
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    equity: Decimal = Field(ge=0)
    daily_pnl: Decimal
    weekly_pnl: Decimal
    peak_equity: Decimal = Field(ge=0)
    drawdown_pct: Decimal = Field(ge=0)


class PortfolioService:
    """Decimal-based paper portfolio ledger and mark-to-market accounting."""
    def __init__(self, initial_capital: Decimal | str | int = Decimal("200000")):
        self.initial_capital = money(initial_capital)
        if self.initial_capital <= 0: raise ValueError("initial capital must be positive")
        self._cash = self.initial_capital; self._positions: dict[str,PortfolioPosition] = {}
        self._realized = ZERO; self._equity_history: list[tuple[datetime,Decimal]] = []
        self._peak_equity = self.initial_capital

    @property
    def cash(self) -> Decimal: return self._cash
    @property
    def positions(self) -> dict[str,PortfolioPosition]: return dict(self._positions)
    @property
    def realized_pnl(self) -> Decimal: return money(self._realized)

    def record_buy(self,symbol: str,quantity: int,price: Decimal | str,at: datetime,fee: Decimal | str = ZERO) -> None:
        price,fee = money(price),money(fee); cost = money(price*quantity+fee)
        if quantity <= 0 or cost > self._cash: raise ValueError("invalid or unaffordable buy")
        old = self._positions.get(symbol); self._cash = money(self._cash-cost)
        if old:
            total = old.quantity+quantity
            average = money((old.average_price*old.quantity+price*quantity)/total)
            self._positions[symbol] = PortfolioPosition(symbol=symbol,quantity=total,average_price=average,
                last_price=price,opened_at=old.opened_at,updated_at=at)
        else:
            self._positions[symbol] = PortfolioPosition(symbol=symbol,quantity=quantity,average_price=price,
                last_price=price,opened_at=at,updated_at=at)

    def record_sell(self,symbol: str,quantity: int,price: Decimal | str,at: datetime,fee: Decimal | str = ZERO) -> Decimal:
        price,fee = money(price),money(fee); old = self._positions.get(symbol)
        if not old or quantity <= 0 or quantity > old.quantity: raise ValueError("invalid sell")
        realized = money((price-old.average_price)*quantity-fee)
        self._realized = money(self._realized+realized); self._cash = money(self._cash+price*quantity-fee)
        remaining = old.quantity-quantity
        if remaining:
            self._positions[symbol] = old.model_copy(update={"quantity":remaining,"last_price":price,"updated_at":at})
        else: del self._positions[symbol]
        return realized

    def snapshot(self,prices: dict[str,Decimal | str],as_of: datetime) -> PortfolioState:
        positions = {}
        for symbol,position in self._positions.items():
            last = money(prices.get(symbol,position.last_price))
            positions[symbol] = position.model_copy(update={"last_price":last,"updated_at":as_of})
        self._positions = positions
        unrealized = money(sum((position.unrealized_pnl for position in positions.values()),ZERO))
        equity = money(self._cash+sum((position.market_value for position in positions.values()),ZERO))
        self._peak_equity = max(self._peak_equity,equity)
        day_start = self._baseline(as_of,weekly=False); week_start = self._baseline(as_of,weekly=True)
        daily_pnl,weekly_pnl = money(equity-day_start),money(equity-week_start)
        drawdown = ZERO if self._peak_equity == 0 else (self._peak_equity-equity)/self._peak_equity
        self._equity_history.append((as_of,equity))
        return PortfolioState(as_of=as_of,initial_capital=self.initial_capital,cash=self._cash,positions=positions,
            realized_pnl=self.realized_pnl,unrealized_pnl=unrealized,equity=equity,daily_pnl=daily_pnl,
            weekly_pnl=weekly_pnl,peak_equity=self._peak_equity,drawdown_pct=drawdown)

    def _baseline(self,as_of: datetime,weekly: bool) -> Decimal:
        candidates=[]; earlier=[]
        for timestamp,equity in self._equity_history:
            same_period = ((timestamp.isocalendar()[:2] == as_of.isocalendar()[:2]) if weekly else timestamp.date()==as_of.date())
            if same_period: candidates.append((timestamp,equity))
            elif timestamp<as_of: earlier.append((timestamp,equity))
        if candidates: return min(candidates,key=lambda item:item[0])[1]
        return max(earlier,key=lambda item:item[0])[1] if earlier else self.initial_capital
