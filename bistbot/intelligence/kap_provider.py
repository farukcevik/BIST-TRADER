from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime,timedelta,timezone
import json
import re
import urllib.request
from typing import Protocol
from bistbot.app.models import EventItem,EventSourceType


class KapProvider(Protocol):
    def fetch(self, symbols: Sequence[str]) -> list[EventItem]: ...


class MockKapProvider:
    provider_mode = "MOCK"
    def __init__(self, events: Sequence[EventItem] = (), error: Exception | None = None):
        self.events, self.error = list(events), error

    def fetch(self, symbols: Sequence[str]) -> list[EventItem]:
        if self.error: raise self.error
        wanted = set(symbols)
        return [event for event in self.events if event.symbol in wanted]


class DisabledKapProvider:
    provider_mode = "DISABLED"
    def fetch(self,symbols: Sequence[str]) -> list[EventItem]: return []


class RealKapProvider:
    provider_mode="REAL"
    URL="https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
    def __init__(self,*,lookback_days: int=3,timeout_seconds: float=15,opener=urllib.request.urlopen,
                 now=lambda:datetime.now(timezone.utc)):
        self.lookback_days,self.timeout_seconds,self.opener,self.now=lookback_days,timeout_seconds,opener,now
        self.availability="AVAILABLE"

    def fetch(self,symbols: Sequence[str]) -> list[EventItem]:
        from bistbot.intelligence.ranking import make_event
        wanted={symbol.removesuffix(".IS") for symbol in symbols}; now=self.now()
        criteria={"fromDate":(now.date()-timedelta(days=self.lookback_days)).isoformat(),"toDate":now.date().isoformat(),
            "memberType":"IGS","mkkMemberOidList":[],"inactiveMkkMemberOidList":[],"disclosureClass":"",
            "subjectList":[],"isLate":"","mainSector":"","sector":"","subSector":"","marketOid":"",
            "index":"","bdkReview":"","bdkMemberOidList":[],"year":"","term":"","ruleType":"",
            "period":"","fromSrc":False,"srcCategory":"","disclosureIndexList":[]}
        request=urllib.request.Request(self.URL,data=json.dumps(criteria).encode(),method="POST",
            headers={"Content-Type":"application/json","User-Agent":"BISTBOT/1.0","Referer":"https://www.kap.org.tr/tr/bildirim-sorgu"})
        try:
            with self.opener(request,timeout=self.timeout_seconds) as response: payload=json.load(response)
            events=[]
            for item in payload:
                codes=_stock_codes(item.get("stockCodes")) | _stock_codes(item.get("relatedStocks"))
                title=str(item.get("summary") or item.get("subject") or "KAP disclosure").strip()
                source_id=str(item.get("disclosureIndex") or "")
                if not source_id: continue
                published=datetime.strptime(item["publishDate"],"%d.%m.%Y %H:%M:%S").replace(tzinfo=ZoneInfo("Europe/Istanbul"))
                for code in sorted(codes & wanted):
                    events.append(make_event(symbol=f"{code}.IS",source="KAP",source_type=EventSourceType.KAP,
                        title=title,body=str(item.get("kapTitle") or ""),published_at=published,fetched_at=now,
                        url=f"https://www.kap.org.tr/tr/Bildirim/{source_id}",trust_score=95,source_id=f"kap:{source_id}:{code}"))
            self.availability="AVAILABLE"; return events
        except Exception:
            self.availability="UNAVAILABLE"; raise


from zoneinfo import ZoneInfo
def _stock_codes(value) -> set[str]:
    if isinstance(value,list): parts=value
    else: parts=re.split(r"[,;/ ]+",str(value or ""))
    return {str(part).strip().upper() for part in parts if re.fullmatch(r"[A-Z0-9]{3,6}",str(part).strip().upper())}
