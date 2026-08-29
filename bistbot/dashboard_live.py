from __future__ import annotations

from datetime import datetime,timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from bistbot.app.models import MarketDataResult
from bistbot.market.calendar import BistTradingCalendar


ISTANBUL=ZoneInfo("Europe/Istanbul")


class QuoteProvider(Protocol):
    def snapshots(self,symbols: list[str]) -> dict[str,MarketDataResult]: ...


def refresh_live_positions(positions: list[dict],provider: QuoteProvider,*,now: datetime|None=None,
                           refresh_seconds: int=30) -> list[dict]:
    """Fetch open-position quotes and calculate display-only values in memory."""
    if not positions:return []
    now=_aware(now or datetime.now(timezone.utc)); symbols=[item["symbol"] for item in positions]
    market_status=BistTradingCalendar().status(now)
    try: results=provider.snapshots(symbols)
    except Exception: results={}
    output=[]
    for persisted in positions:
        item=dict(persisted); result=results.get(item["symbol"]); timestamp=None
        if result is not None and result.available and result.snapshot is not None:
            price=float(result.snapshot.price); timestamp=_aware(result.snapshot.timestamp)
            age=max(0,(now-timestamp).total_seconds())
            fresh_limit=max(60,refresh_seconds*2)
            status=("LIVE / FRESH" if age<=fresh_limit else "DELAYED" if age<=900 else "STALE") if market_status.can_execute_orders else "LAST_CLOSE / MARKET_CLOSED"
            fallback=False
        else:
            price=float(item["last_price"]); age=None; status="STALE FALLBACK"; fallback=True
        entry=float(item["average_entry"]); quantity=int(item["quantity"])
        item.update({"live_price":price,"live_market_value":price*quantity,
            "live_unrealized_pnl":(price-entry)*quantity,
            "live_unrealized_pnl_pct":((price/entry)-1)*100,
            "price_timestamp":timestamp.astimezone(ISTANBUL).isoformat() if timestamp else None,
            "price_timestamp_display":timestamp.astimezone(ISTANBUL).strftime("%d.%m.%Y %H:%M:%S") if timestamp else "Persisted DB price",
            "price_age_seconds":age,"price_age":_format_age(age),"price_status":status,
            "uses_persisted_fallback":fallback})
        output.append(item)
    return output


def live_portfolio_summary(*,cash: float,positions: list[dict],initial_capital: float,
                           bot_recorded_equity: float) -> dict:
    market_value=sum(item["live_market_value"] for item in positions)
    unrealized=sum(item["live_unrealized_pnl"] for item in positions)
    equity=cash+market_value
    return {"live_equity":equity,"live_unrealized":unrealized,
        "live_return_pct":((equity/initial_capital)-1)*100 if initial_capital else 0,
        "bot_recorded_equity":bot_recorded_equity,"live_invested":market_value}


def _format_age(seconds: float|None) -> str:
    if seconds is None:return "unknown"
    value=int(seconds)
    if value<60:return f"{value} sec"
    if value<3600:return f"{value//60} min {value%60} sec"
    return f"{value//3600} h {(value%3600)//60} min"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
