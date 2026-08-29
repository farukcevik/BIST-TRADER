from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

BIST_TIMEZONE = ZoneInfo("Europe/Istanbul")

class MarketSession(StrEnum):
    CLOSED="CLOSED"; PRE_OPEN="PRE_OPEN"; OPENING_AUCTION="OPENING_AUCTION"
    CONTINUOUS_TRADING="CONTINUOUS_TRADING"; CLOSING_AUCTION="CLOSING_AUCTION"; POST_CLOSE="POST_CLOSE"

class MarketClosedError(PermissionError):
    def __init__(self,reason: str): self.reason=reason; super().__init__(reason)

@dataclass(frozen=True)
class MarketStatus:
    local_time: datetime; is_trading_day: bool; is_weekend: bool; is_official_holiday: bool
    is_half_day: bool; current_session: MarketSession; can_execute_orders: bool; reason: str
    def as_dict(self)->dict:
        return {"local_time":self.local_time.isoformat(),"timezone":"Europe/Istanbul",
            "trading_day":self.is_trading_day,"is_weekend":self.is_weekend,
            "is_official_holiday":self.is_official_holiday,"is_half_day":self.is_half_day,
            "session":self.current_session.value,"can_execute_orders":self.can_execute_orders,"reason":self.reason}

# Explicit Borsa Istanbul Pay Piyasasi 2026 official holiday table.
FULL_DAY_HOLIDAYS_2026={date(2026,1,1),date(2026,3,20),date(2026,3,21),date(2026,3,22),
    date(2026,4,23),date(2026,5,1),date(2026,5,19),date(2026,5,27),date(2026,5,28),
    date(2026,5,29),date(2026,5,30),date(2026,7,15),date(2026,8,30),date(2026,10,29)}
HALF_DAY_HOLIDAYS_2026={date(2026,3,19),date(2026,5,26),date(2026,10,28)}

class BistTradingCalendar:
    """Single authoritative, fail-closed BIST Pay Market session service."""
    def __init__(self,*,full_day_holidays:set[date]|None=None,half_day_holidays:set[date]|None=None,
                 supported_years:set[int]|None=None):
        self.full_day_holidays=FULL_DAY_HOLIDAYS_2026 if full_day_holidays is None else full_day_holidays
        self.half_day_holidays=HALF_DAY_HOLIDAYS_2026 if half_day_holidays is None else half_day_holidays
        self.supported_years={2026} if supported_years is None else supported_years

    def status(self,at:datetime|None=None)->MarketStatus:
        local=(at or datetime.now(BIST_TIMEZONE)).astimezone(BIST_TIMEZONE); day=local.date(); weekend=day.weekday()>=5
        if weekend:return self._closed(local,weekend,day in self.full_day_holidays,False,"MARKET_CLOSED_WEEKEND")
        if day.year not in self.supported_years:return self._closed(local,False,False,False,"MARKET_CALENDAR_UNAVAILABLE")
        if day in self.full_day_holidays:return self._closed(local,False,True,False,"MARKET_CLOSED_HOLIDAY")
        half=day in self.half_day_holidays; current=local.timetz().replace(tzinfo=None)
        close=time(12,30) if half else time(18,0); auction_end=time(12,40) if half else time(18,10)
        if current<time(9,40):session=MarketSession.CLOSED
        elif current<time(9,55):session=MarketSession.PRE_OPEN
        elif current<time(10,0):session=MarketSession.OPENING_AUCTION
        elif current<close:session=MarketSession.CONTINUOUS_TRADING
        elif current<auction_end:session=MarketSession.CLOSING_AUCTION
        else:session=MarketSession.POST_CLOSE
        executable=session is MarketSession.CONTINUOUS_TRADING
        return MarketStatus(local,True,False,False,half,session,executable,
            "MARKET_OPEN_CONTINUOUS" if executable else f"MARKET_CLOSED_{session.value}")

    @staticmethod
    def _closed(local,weekend,holiday,half,reason):
        return MarketStatus(local,False,weekend,holiday,half,MarketSession.CLOSED,False,reason)

    def require_executable(self,at:datetime|None=None)->MarketStatus:
        status=self.status(at)
        if not status.can_execute_orders:raise MarketClosedError(status.reason)
        return status

    def is_fresh_session_price(self,price_timestamp:datetime,at:datetime|None=None,
                               max_age:timedelta=timedelta(minutes=5))->bool:
        now=(at or datetime.now(BIST_TIMEZONE)).astimezone(BIST_TIMEZONE)
        stamp=price_timestamp.astimezone(BIST_TIMEZONE) if price_timestamp.tzinfo else price_timestamp.replace(tzinfo=BIST_TIMEZONE)
        status=self.status(now)
        return (status.can_execute_orders and stamp.date()==now.date() and stamp.time()>=time(10,0)
                and stamp<=now and now-stamp<=max_age)
