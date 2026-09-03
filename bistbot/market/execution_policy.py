from __future__ import annotations
from datetime import datetime,time
from decimal import Decimal,InvalidOperation
from bistbot.app.models import ExecutionQuoteValidation,QuoteFreshnessStatus
from bistbot.market.calendar import BIST_TIMEZONE,BistTradingCalendar,MarketSession

YAHOO_PROVIDERS=frozenset({"YahooBistProvider","YAHOO"})
DEFAULT_EXECUTION_FRESHNESS_SECONDS=5*60

def validate_execution_quote(quote,*,evaluated_at:datetime,calendar:BistTradingCalendar,
        default_freshness_seconds:int|float,yahoo_freshness_seconds:int|float)->ExecutionQuoteValidation:
    if default_freshness_seconds<=0 or yahoo_freshness_seconds<=0: raise ValueError("freshness limits must be positive")
    now=evaluated_at.replace(tzinfo=BIST_TIMEZONE) if evaluated_at.tzinfo is None else evaluated_at.astimezone(BIST_TIMEZONE)
    provider=str(getattr(quote,"provider","") or "UNKNOWN")
    limit=float(yahoo_freshness_seconds if provider in YAHOO_PROVIDERS else default_freshness_seconds)
    raw=getattr(quote,"source_timestamp",None)
    stamp=None if raw is None else (raw.replace(tzinfo=BIST_TIMEZONE) if raw.tzinfo is None else raw.astimezone(BIST_TIMEZONE))
    market=calendar.status(now); session_date=stamp.date() if stamp else None
    age=None if stamp is None else max(0.0,(now-stamp).total_seconds())
    try:
        price=Decimal(str(getattr(quote,"price",None))); valid_price=price.is_finite() and price>0
    except (InvalidOperation,TypeError,ValueError): price=None; valid_price=False
    if stamp is None: status=QuoteFreshnessStatus.NO_TIMESTAMP; reason="NO_TIMESTAMP"
    elif stamp.date()!=now.date() or stamp.time()<time(10): status=QuoteFreshnessStatus.PREVIOUS_SESSION; reason="PREVIOUS_SESSION"
    elif market.current_session is not MarketSession.CONTINUOUS_TRADING or not market.is_trading_day or stamp>now:
        status=QuoteFreshnessStatus.STALE_CURRENT_SESSION; reason=market.reason if stamp<=now else "FUTURE_TIMESTAMP"
    elif age is None or age>limit: status=QuoteFreshnessStatus.STALE_CURRENT_SESSION; reason="STALE_CURRENT_SESSION"
    elif provider in YAHOO_PROVIDERS and age>float(default_freshness_seconds):
        status=QuoteFreshnessStatus.CURRENT_SESSION_DELAYED_ACCEPTED; reason=status.value
    else: status=QuoteFreshnessStatus.FRESH; reason=status.value
    valid=status in {QuoteFreshnessStatus.FRESH,QuoteFreshnessStatus.CURRENT_SESSION_DELAYED_ACCEPTED} and valid_price
    if not valid_price: reason="INVALID_EXECUTION_PRICE"
    return ExecutionQuoteValidation(valid=valid,provider=provider,price=price if valid_price else None,
        source_timestamp=stamp,evaluated_at=now,session_date=session_date,quote_age_seconds=age,
        freshness_limit_seconds=limit,freshness_status=status,reason=reason)
