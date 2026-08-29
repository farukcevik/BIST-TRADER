from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime,timedelta,timezone
import json
import re
import time
import urllib.request
from typing import Protocol
from bistbot.app.models import EventItem,EventSourceType,IntelligenceProviderDiagnostics,ProviderState


class KapProvider(Protocol):
    def fetch(self, symbols: Sequence[str]) -> list[EventItem]: ...


class MockKapProvider:
    provider_mode = "MOCK"
    def __init__(self, events: Sequence[EventItem] = (), error: Exception | None = None):
        self.events, self.error = list(events), error
        self.availability="AVAILABLE_WITH_EVENTS" if events else "AVAILABLE_NO_EVENTS"

    def fetch(self, symbols: Sequence[str]) -> list[EventItem]:
        if self.error: raise self.error
        wanted = set(symbols)
        return [event for event in self.events if event.symbol in wanted]


class DisabledKapProvider:
    provider_mode = "DISABLED"
    availability="UNAVAILABLE"
    def fetch(self,symbols: Sequence[str]) -> list[EventItem]: return []


class RealKapProvider:
    provider_mode="REAL"
    URL="https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
    def __init__(self,*,lookback_days: int=3,timeout_seconds: float=15,opener=urllib.request.urlopen,
                 now=lambda:datetime.now(timezone.utc),max_attempts: int=2,sleeper=time.sleep):
        self.lookback_days,self.timeout_seconds,self.opener,self.now=lookback_days,timeout_seconds,opener,now
        self.max_attempts,self.sleeper=max_attempts,sleeper; self.availability=ProviderState.UNAVAILABLE.value
        self.last_diagnostics=IntelligenceProviderDiagnostics(provider=type(self).__name__,mode=self.provider_mode,
            endpoint=self.URL,status=ProviderState.UNAVAILABLE)

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
        started=time.monotonic(); retries=0; requested_at=now
        try:
            for attempt in range(1,self.max_attempts+1):
                try:
                    with self.opener(request,timeout=self.timeout_seconds) as response:
                        status=getattr(response,"status",200); raw=response.read()
                    payload=json.loads(raw.decode("utf-8")); break
                except Exception:
                    if attempt>=self.max_attempts: raise
                    retries+=1; self.sleeper(min(.25*2**(attempt-1),1))
            if not isinstance(payload,list): raise ValueError("KAP response is not a disclosure list")
            events=[]; parsed_count=0
            for item in payload:
                codes=_stock_codes(item.get("stockCodes")) | _stock_codes(item.get("relatedStocks"))
                title=str(item.get("summary") or item.get("subject") or "KAP disclosure").strip()
                source_id=str(item.get("disclosureIndex") or "")
                if not source_id: continue
                published=datetime.strptime(item["publishDate"],"%d.%m.%Y %H:%M:%S").replace(tzinfo=ZoneInfo("Europe/Istanbul"))
                parsed_count+=1
                for code in sorted(codes & wanted):
                    events.append(make_event(symbol=f"{code}.IS",source="KAP",source_type=EventSourceType.KAP,
                        title=title,body=str(item.get("kapTitle") or ""),published_at=published,fetched_at=now,
                        url=f"https://www.kap.org.tr/tr/Bildirim/{source_id}",trust_score=95,source_id=f"kap:{source_id}:{code}"))
            events=list({event.id:event for event in events}.values())
            state=ProviderState.AVAILABLE_WITH_EVENTS if events else ProviderState.AVAILABLE_NO_EVENTS
            self.availability=state.value
            self.last_diagnostics=IntelligenceProviderDiagnostics(provider=type(self).__name__,mode=self.provider_mode,
                endpoint=self.URL,request_timestamp=requested_at,http_status=status,response_size=len(raw),
                raw_events=len(payload),parsed_events=parsed_count,mapped_events=len(events),symbols_matched=len({e.symbol for e in events}),
                latency_ms=(time.monotonic()-started)*1000,retry_count=retries,status=state,
                latest_event=max((e.published_at for e in events),default=None))
            return events
        except Exception as error:
            state=ProviderState.ERROR if isinstance(error,(ValueError,KeyError,TypeError,json.JSONDecodeError)) else ProviderState.UNAVAILABLE
            self.availability=state.value
            self.last_diagnostics=IntelligenceProviderDiagnostics(provider=type(self).__name__,mode=self.provider_mode,
                endpoint=self.URL,request_timestamp=requested_at,http_status=locals().get("status"),
                response_size=len(locals().get("raw",b"")),latency_ms=(time.monotonic()-started)*1000,
                retry_count=retries,status=state,error_type=type(error).__name__,error_message=str(error).split("?")[0][:200])
            raise


from zoneinfo import ZoneInfo
def _stock_codes(value) -> set[str]:
    if isinstance(value,list): parts=value
    else: parts=re.split(r"[,;/ ]+",str(value or ""))
    return {str(part).strip().upper() for part in parts if re.fullmatch(r"[A-Z0-9]{3,6}",str(part).strip().upper())}
