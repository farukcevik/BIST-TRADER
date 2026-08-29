from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime
import json
import re
import ssl
import time
from typing import Callable,Protocol
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

import certifi

from bistbot.app.models import MacroEvent,TimestampSource
from .materiality import canonical_event_id,category_for_text


class MacroNewsProvider(Protocol):
    """Reliable macro/agenda source. Implementations must not use social media as confirmation."""
    provider_mode: str
    def fetch(self) -> list[MacroEvent]: ...


class StaticMacroNewsProvider:
    provider_mode = "STATIC"
    def __init__(self, events: list[MacroEvent] | None = None): self.events = events or []
    def fetch(self) -> list[MacroEvent]: return list(self.events)


class RealMacroNewsProvider:
    """Read-only official macro feeds plus public market-context observations."""
    provider_mode = "REAL"
    OFFICIAL_FEEDS = {
        "TCMB": "https://www.tcmb.gov.tr/wps/wcm/connect/TR/TCMB%2BTR/Bottom%2BMenu/Diger/RSS/Basin%2BDuyurulari",
        "Federal Reserve": "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "ECB": "https://www.ecb.europa.eu/rss/press.html",
    }
    DECLARED_UNAVAILABLE = ("SPK","Borsa Istanbul","TUIK","Treasury and Finance Ministry")
    MARKET_SYMBOLS = {"USDTRY":"TRY=X","EURTRY":"EURTRY=X","Brent":"BZ=F","Gold":"GC=F",
        "S&P 500":"^GSPC","Nasdaq":"^IXIC","VIX":"^VIX","US 10Y":"^TNX"}
    SHOCK_THRESHOLDS = {"USDTRY":2.5,"EURTRY":2.5,"Brent":5,"Gold":4,"S&P 500":-4,
        "Nasdaq":-4.5,"VIX":25,"US 10Y":8}

    def __init__(self,*,opener=urllib.request.urlopen,timeout_seconds: float=10,
                 now: Callable[[],datetime]|None=None):
        self.opener=opener; self.timeout_seconds=timeout_seconds; self.now=now or (lambda:datetime.now(timezone.utc))
        self.ssl_context=ssl.create_default_context(cafile=certifi.where()); self.status="UNAVAILABLE"
        self.last_diagnostics={"provider":type(self).__name__,"provider_status":"UNAVAILABLE","sources":{}}

    def fetch(self) -> list[MacroEvent]:
        started=time.monotonic(); fetched_at=self.now(); events=[]; sources={}
        jobs={**{f"feed:{name}":url for name,url in self.OFFICIAL_FEEDS.items()},
              **{f"market:{name}":symbol for name,symbol in self.MARKET_SYMBOLS.items()}}
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures={pool.submit(self._feed,name[5:],value,fetched_at) if name.startswith("feed:")
                     else pool.submit(self._market,name[7:],value,fetched_at):name for name,value in jobs.items()}
            for future in as_completed(futures):
                name=futures[future]
                try:
                    found=future.result(); events.extend(found); sources[name]={"status":"AVAILABLE","events":len(found)}
                except Exception as error:
                    sources[name]={"status":"UNAVAILABLE","events":0,"error":f"{type(error).__name__}: {str(error)[:160]}"}
        for name in self.DECLARED_UNAVAILABLE:
            sources[f"official:{name}"]={"status":"UNAVAILABLE","events":0,
                "error":"No stable public machine-readable endpoint configured; no data fabricated"}
        available=sum(item["status"]=="AVAILABLE" for item in sources.values())
        self.status="REAL" if available==len(sources) else "PARTIAL" if available else "UNAVAILABLE"
        unique={event.canonical_event_id:event for event in events}; output=sorted(unique.values(),key=lambda e:e.published_at,reverse=True)
        self.last_diagnostics={"provider":type(self).__name__,"provider_status":self.status,
            "events_fetched":len(output),"sources":sources,"latency_ms":round((time.monotonic()-started)*1000,2),
            "request_timestamp":fetched_at.isoformat()}
        return output

    def _open(self,url: str):
        request=urllib.request.Request(url,headers={"User-Agent":"BISTBOT/1.0 (+paper-market-regime)"})
        try:return self.opener(request,timeout=self.timeout_seconds,context=self.ssl_context)
        except TypeError:return self.opener(request,timeout=self.timeout_seconds)

    def _feed(self,source: str,url: str,fetched_at: datetime) -> list[MacroEvent]:
        with self._open(url) as response: raw=response.read()
        root=ET.fromstring(raw); output=[]
        items=root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for index,item in enumerate(items[:30]):
            def value(*names):
                for name in names:
                    node=item.find(name)
                    if node is not None and node.text:return node.text.strip()
                return ""
            title=value("title","{http://www.w3.org/2005/Atom}title"); body=value("description","summary","{http://www.w3.org/2005/Atom}summary")
            link=value("link")
            if not link:
                node=item.find("{http://www.w3.org/2005/Atom}link"); link=node.get("href","") if node is not None else ""
            date=value("pubDate","date","{http://purl.org/dc/elements/1.1/}date","{http://www.w3.org/2005/Atom}updated")
            published,timestamp_source=_parse_date(date,fetched_at)
            if timestamp_source is TimestampSource.FETCH_FALLBACK:
                for node in item.iter():
                    candidate=(node.text or "").strip()
                    parsed,kind=_parse_date(candidate,fetched_at)
                    if kind is not TimestampSource.FETCH_FALLBACK:published,timestamp_source=parsed,kind; break
            source_id=link or f"{source}:{published.isoformat()}:{index}"
            if title: output.append(MacroEvent(canonical_event_id=canonical_event_id(source,source_id,title),
                source_id=source_id,source=source,title=title,body=body,url=link or None,published_at=published,
                fetched_at=fetched_at,timestamp_source=timestamp_source,
                category=category_for_text(f"{title} {body}"),source_reliability=98))
        return output

    def _market(self,name: str,symbol: str,fetched_at: datetime) -> list[MacroEvent]:
        url=f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=5d&interval=1d"
        with self._open(url) as response: payload=json.load(response)
        result=payload["chart"]["result"][0]; closes=[x for x in result["indicators"]["quote"][0]["close"] if x is not None]
        if len(closes)<2:return []
        change=(closes[-1]/closes[-2]-1)*100; threshold=self.SHOCK_THRESHOLDS[name]
        shocked=change>=threshold if threshold>0 else change<=threshold
        if not shocked:return []
        kind=("FX shock" if name in {"USDTRY","EURTRY"} else "oil shock" if name=="Brent" else
              "market crash" if name in {"S&P 500","Nasdaq"} else "VIX shock" if name=="VIX" else "yield shock")
        title=f"{name} {kind}: daily move {change:+.2f}%"; source_id=f"yahoo:{symbol}:{result['timestamp'][-1]}"
        return [MacroEvent(canonical_event_id=canonical_event_id("Yahoo Market Context",source_id,title),source_id=source_id,
            source="Yahoo Market Context",title=title,url=url,published_at=datetime.fromtimestamp(result["timestamp"][-1],timezone.utc),
            fetched_at=fetched_at,timestamp_source=TimestampSource.SOURCE,
            category=category_for_text(title),source_reliability=75,direction="NEGATIVE")]


def _parse_date(value: str,fallback: datetime) -> tuple[datetime,TimestampSource]:
    if not value:return fallback,TimestampSource.FETCH_FALLBACK
    try:
        parsed=parsedate_to_datetime(value)
    except (TypeError,ValueError):
        try:parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
        except ValueError:
            parsed=_parse_turkish_date(value)
            if parsed is None:return fallback,TimestampSource.FETCH_FALLBACK
            return parsed,TimestampSource.PARSED_PAGE
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)),TimestampSource.SOURCE


def _parse_turkish_date(value: str) -> datetime|None:
    months={"oca":1,"şub":2,"sub":2,"mar":3,"nis":4,"may":5,"haz":6,"tem":7,"ağu":8,"agu":8,"eyl":9,"eki":10,"kas":11,"ara":12}
    match=re.search(r"\b(\d{1,2})\s+(Oca|Şub|Sub|Mar|Nis|May|Haz|Tem|Ağu|Agu|Eyl|Eki|Kas|Ara)[a-zçğıöşü]*\s+(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?",value,re.I)
    if not match:return None
    day,month,year,hour,minute,second=match.groups()
    return datetime(int(year),months[month.lower()[:3]],int(day),int(hour or 0),int(minute or 0),int(second or 0),tzinfo=timezone.utc)


class CompositeMacroNewsProvider:
    """Failure-isolated aggregation of official/licensed providers."""
    provider_mode = "COMPOSITE"
    def __init__(self, providers: list[MacroNewsProvider]): self.providers = providers; self.errors: list[str] = []
    def fetch(self) -> list[MacroEvent]:
        output: dict[str, MacroEvent] = {}; self.errors = []
        for provider in self.providers:
            try:
                for event in provider.fetch(): output[event.canonical_event_id] = event
            except Exception as error:
                self.errors.append(f"{type(provider).__name__}: {type(error).__name__}")
        return sorted(output.values(), key=lambda item:item.published_at, reverse=True)
