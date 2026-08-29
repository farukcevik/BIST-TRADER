from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime,timezone
import json
import time
import urllib.parse
import urllib.request
from typing import Protocol
from bistbot.app.models import EventItem,EventSourceType,IntelligenceProviderDiagnostics,ProviderState


class NewsProvider(Protocol):
    def fetch(self, symbols: Sequence[str]) -> list[EventItem]: ...


class MockNewsProvider:
    provider_mode = "MOCK"
    def __init__(self, events: Sequence[EventItem] = (), error: Exception | None = None):
        self.events, self.error = list(events), error
        self.availability="AVAILABLE_WITH_EVENTS" if events else "AVAILABLE_NO_EVENTS"

    def fetch(self, symbols: Sequence[str]) -> list[EventItem]:
        if self.error: raise self.error
        wanted = set(symbols)
        return [event for event in self.events if event.symbol in wanted]


class DisabledNewsProvider:
    provider_mode = "DISABLED"
    availability="UNAVAILABLE"
    def fetch(self,symbols: Sequence[str]) -> list[EventItem]: return []


class YahooFinanceNewsProvider:
    provider_mode="REAL"
    def __init__(self,*,timeout_seconds: float=10,news_count: int=5,opener=urllib.request.urlopen,
                 now=lambda:datetime.now(timezone.utc),max_attempts: int=2,sleeper=time.sleep):
        self.timeout_seconds,self.news_count,self.opener,self.now=timeout_seconds,news_count,opener,now
        self.max_attempts,self.sleeper=max_attempts,sleeper
        self.availability=ProviderState.UNAVAILABLE.value
        self.last_diagnostics=IntelligenceProviderDiagnostics(provider=type(self).__name__,mode=self.provider_mode,
            endpoint="https://query1.finance.yahoo.com/v1/finance/search",status=ProviderState.UNAVAILABLE)

    def fetch(self,symbols: Sequence[str]) -> list[EventItem]:
        from bistbot.intelligence.ranking import make_event
        events=[]; failures=[]; fetched_at=self.now(); raw_count=0; parsed_count=0; response_size=0; http_status=None; retries=0
        started=time.monotonic()
        for symbol in symbols:
            query=urllib.parse.urlencode({"q":symbol,"quotesCount":0,"newsCount":self.news_count})
            request=urllib.request.Request(f"https://query1.finance.yahoo.com/v1/finance/search?{query}",
                headers={"User-Agent":"Mozilla/5.0 BISTBOT/1.0"})
            payload=None
            for attempt in range(1,self.max_attempts+1):
              try:
                with self.opener(request,timeout=self.timeout_seconds) as response:
                    http_status=getattr(response,"status",200); raw=response.read()
                response_size+=len(raw); payload=json.loads(raw.decode("utf-8")); break
              except Exception as error:
                if attempt<self.max_attempts: retries+=1; self.sleeper(min(.25*2**(attempt-1),1))
                else: failures.append(error)
            if payload is None: continue
            try:
                raw_items=payload.get("news",[]); raw_count+=len(raw_items)
                for item in raw_items:
                    source_id=str(item.get("uuid") or item.get("id") or "")
                    title=str(item.get("title") or "").strip(); published=item.get("providerPublishTime")
                    if not source_id or not title or not published: continue
                    parsed_count+=1
                    related={str(value).upper() for value in item.get("relatedTickers",[])}
                    if symbol.upper() not in related and symbol.removesuffix(".IS").upper() not in related: continue
                    events.append(make_event(symbol=symbol,source=str(item.get("publisher") or "Yahoo Finance"),
                        source_type=EventSourceType.NEWS,title=title,body=str(item.get("summary") or ""),
                        url=item.get("link"),published_at=datetime.fromtimestamp(published,timezone.utc),
                        fetched_at=fetched_at,trust_score=65,source_id=f"yahoo:{source_id}:{symbol}"))
            except Exception as error: failures.append(error)
        unique={event.id:event for event in events}; events=list(unique.values())
        if failures and not events and len(failures)>=len(symbols):
            state=(ProviderState.ERROR if any(isinstance(e,(ValueError,TypeError,KeyError,json.JSONDecodeError)) for e in failures)
                   else ProviderState.UNAVAILABLE)
        else: state=ProviderState.AVAILABLE_WITH_EVENTS if events else ProviderState.AVAILABLE_NO_EVENTS
        error=failures[-1] if failures else None; self.availability=state.value
        self.last_diagnostics=IntelligenceProviderDiagnostics(provider=type(self).__name__,mode=self.provider_mode,
            endpoint="query1.finance.yahoo.com/v1/finance/search",request_timestamp=fetched_at,http_status=http_status,
            response_size=response_size,raw_events=raw_count,parsed_events=parsed_count,mapped_events=len(events),
            symbols_matched=len({event.symbol for event in events}),latency_ms=(time.monotonic()-started)*1000,
            retry_count=retries,status=state,latest_event=max((e.published_at for e in events),default=None),
            error_type=type(error).__name__ if error else None,error_message=_safe_error(error))
        if state in {ProviderState.UNAVAILABLE,ProviderState.ERROR}: raise failures[0]
        return events


def _safe_error(error: Exception | None) -> str | None:
    if error is None: return None
    return str(error).split("?")[0][:200]
