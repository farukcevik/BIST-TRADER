from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime,timezone
import json
import urllib.parse
import urllib.request
from typing import Protocol
from bistbot.app.models import EventItem,EventSourceType


class NewsProvider(Protocol):
    def fetch(self, symbols: Sequence[str]) -> list[EventItem]: ...


class MockNewsProvider:
    provider_mode = "MOCK"
    def __init__(self, events: Sequence[EventItem] = (), error: Exception | None = None):
        self.events, self.error = list(events), error

    def fetch(self, symbols: Sequence[str]) -> list[EventItem]:
        if self.error: raise self.error
        wanted = set(symbols)
        return [event for event in self.events if event.symbol in wanted]


class DisabledNewsProvider:
    provider_mode = "DISABLED"
    def fetch(self,symbols: Sequence[str]) -> list[EventItem]: return []


class YahooFinanceNewsProvider:
    provider_mode="REAL"
    def __init__(self,*,timeout_seconds: float=10,news_count: int=5,opener=urllib.request.urlopen,
                 now=lambda:datetime.now(timezone.utc)):
        self.timeout_seconds,self.news_count,self.opener,self.now=timeout_seconds,news_count,opener,now
        self.availability="AVAILABLE"

    def fetch(self,symbols: Sequence[str]) -> list[EventItem]:
        from bistbot.intelligence.ranking import make_event
        events=[]; failures=[]; fetched_at=self.now()
        for symbol in symbols:
            query=urllib.parse.urlencode({"q":symbol,"quotesCount":0,"newsCount":self.news_count})
            request=urllib.request.Request(f"https://query1.finance.yahoo.com/v1/finance/search?{query}",
                headers={"User-Agent":"Mozilla/5.0 BISTBOT/1.0"})
            try:
                with self.opener(request,timeout=self.timeout_seconds) as response: payload=json.load(response)
                for item in payload.get("news",[]):
                    related={str(value).upper() for value in item.get("relatedTickers",[])}
                    if symbol.upper() not in related and symbol.removesuffix(".IS").upper() not in related: continue
                    source_id=str(item.get("uuid") or item.get("id") or "")
                    title=str(item.get("title") or "").strip(); published=item.get("providerPublishTime")
                    if not source_id or not title or not published: continue
                    events.append(make_event(symbol=symbol,source=str(item.get("publisher") or "Yahoo Finance"),
                        source_type=EventSourceType.NEWS,title=title,body=str(item.get("summary") or ""),
                        url=item.get("link"),published_at=datetime.fromtimestamp(published,timezone.utc),
                        fetched_at=fetched_at,trust_score=65,source_id=f"yahoo:{source_id}:{symbol}"))
            except Exception as error: failures.append(error)
        if failures and len(failures)==len(symbols): self.availability="UNAVAILABLE"; raise failures[0]
        self.availability="AVAILABLE" if not failures else "DEGRADED"
        return events
