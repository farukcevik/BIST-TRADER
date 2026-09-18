from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime,timedelta,timezone
from email.utils import parsedate_to_datetime
import json
import random
import re
import time
from typing import Protocol
from urllib.error import HTTPError
import urllib.request
from uuid import uuid4
from zoneinfo import ZoneInfo

from bistbot.app.models import EventItem,EventSourceType,IntelligenceProviderDiagnostics,ProviderState
from bistbot.storage.database import Database


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


class KapRateLimited(RuntimeError):
    pass

class KapLeaseLost(RuntimeError):
    pass


class RealKapProvider:
    """One global incremental disclosure poll backed by a durable 90-day cache.

    The request is never scoped to scanner symbols. Symbols only filter the cached
    result after the global poll has been persisted, so changing Top N cannot create
    additional KAP traffic or gaps in the disclosure cursor.
    """
    provider_mode="REAL"
    URL="https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
    def __init__(self,*,database: Database|None=None,lookback_days: int=3,retention_days: int=90,
                 timeout_seconds: float=15,opener=urllib.request.urlopen,now=lambda:datetime.now(timezone.utc),
                 max_attempts: int=2,sleeper=time.sleep,base_backoff_seconds: float=30,
                 max_backoff_seconds: float=900,jitter=random.uniform):
        self.database=database
        self.lookback_days,self.retention_days=lookback_days,retention_days
        self.timeout_seconds,self.opener,self.now=timeout_seconds,opener,now
        self.max_attempts,self.sleeper=max_attempts,sleeper
        self.base_backoff_seconds,self.max_backoff_seconds=base_backoff_seconds,max_backoff_seconds
        self.jitter=jitter; self.availability=ProviderState.UNAVAILABLE.value
        self._memory_raw: dict[str,dict]={}; self._memory_state={}
        self.last_diagnostics=IntelligenceProviderDiagnostics(provider=type(self).__name__,mode=self.provider_mode,
            endpoint=self.URL,status=ProviderState.UNAVAILABLE)

    def fetch(self,symbols: Sequence[str]) -> list[EventItem]:
        now=_aware(self.now()); state=self._state(); cooldown=_parse_time(state.get("cooldown_until"))
        if cooldown and now < cooldown:
            events=self._cached_events(symbols,now)
            self.availability=ProviderState.UNAVAILABLE.value
            self.last_diagnostics=self._diagnostics(now,ProviderState.UNAVAILABLE,events,
                cooldown_until=cooldown,error_type="KAP_COOLDOWN",error_message="rate-limit cooldown active")
            return events
        lease_id,state,blocked=self._claim_lease(now)
        if lease_id is None:
            events=self._cached_events(symbols,now); cooldown=_parse_time(state.get("cooldown_until"))
            self.availability=ProviderState.UNAVAILABLE.value
            self.last_diagnostics=self._diagnostics(now,ProviderState.UNAVAILABLE,events,
                cooldown_until=cooldown,cursor_published_at=_parse_time(state.get("cursor_published_at")),
                error_type=blocked,error_message=("global KAP poll owned by another caller"
                    if blocked=="KAP_POLL_IN_PROGRESS" else "rate-limit cooldown active"))
            return events
        cursor=_parse_time(state.get("cursor_published_at"))
        backfill_from=cursor or now-timedelta(days=self.lookback_days)
        request=self._request(backfill_from,now); started=time.monotonic(); retries=0
        try:
            for attempt in range(1,self.max_attempts+1):
                try:
                    with self.opener(request,timeout=self.timeout_seconds) as response:
                        status=getattr(response,"status",200); raw=response.read(); headers=getattr(response,"headers",{})
                    if status == 429: raise HTTPError(self.URL,429,"Too Many Requests",headers,None)
                    payload=json.loads(raw.decode("utf-8")); break
                except HTTPError as error:
                    if error.code == 429:
                        cooldown_until=self._rate_limit(now,error.headers,state,lease_id)
                        events=self._cached_events(symbols,now)
                        self.availability=ProviderState.UNAVAILABLE.value
                        self.last_diagnostics=self._diagnostics(now,ProviderState.UNAVAILABLE,events,http_status=429,
                            latency_ms=(time.monotonic()-started)*1000,cooldown_until=cooldown_until,
                            cursor_published_at=cursor,backfill_from=backfill_from,error_type="HTTPError",
                            error_message="KAP HTTP 429")
                        return events
                    if attempt>=self.max_attempts: raise
                    retries+=1; self.sleeper(self._retry_delay(attempt))
                except Exception:
                    if attempt>=self.max_attempts: raise
                    retries+=1; self.sleeper(self._retry_delay(attempt))
            if not isinstance(payload,list): raise ValueError("KAP response is not a disclosure list")
            parsed=[item for item in payload if self._disclosure_id(item) and item.get("publishDate")]
            latest=max([value for value in (cursor,now if not parsed else None) if value is not None]
                +[_published(item) for item in parsed])
            cleanup_deleted=self._persist_success(parsed,now,latest,lease_id)
            events=self._cached_events(symbols,now)
            state_value=ProviderState.AVAILABLE_WITH_EVENTS if events else ProviderState.AVAILABLE_NO_EVENTS
            self.availability=state_value.value
            self.last_diagnostics=self._diagnostics(now,state_value,events,http_status=status,
                response_size=len(raw),raw_events=len(payload),parsed_events=len(parsed),retry_count=retries,
                latency_ms=(time.monotonic()-started)*1000,cursor_published_at=latest,
                backfill_from=backfill_from,cleanup_deleted=cleanup_deleted)
            return events
        except Exception as error:
            self._record_failure(now,error,state,lease_id)
            self.availability=ProviderState.ERROR.value if isinstance(error,(ValueError,KeyError,TypeError,json.JSONDecodeError)) else ProviderState.UNAVAILABLE.value
            provider_state=ProviderState(self.availability)
            self.last_diagnostics=self._diagnostics(now,provider_state,[],http_status=locals().get("status"),
                response_size=len(locals().get("raw",b"")),latency_ms=(time.monotonic()-started)*1000,
                retry_count=retries,cursor_published_at=cursor,backfill_from=backfill_from,
                error_type=type(error).__name__,error_message=str(error).split("?")[0][:200])
            raise

    def _request(self,start: datetime,end: datetime):
        criteria={"fromDate":start.date().isoformat(),"toDate":end.date().isoformat(),"memberType":"IGS",
            "mkkMemberOidList":[],"inactiveMkkMemberOidList":[],"disclosureClass":"","subjectList":[],
            "isLate":"","mainSector":"","sector":"","subSector":"","marketOid":"","index":"",
            "bdkReview":"","bdkMemberOidList":[],"year":"","term":"","ruleType":"","period":"",
            "fromSrc":False,"srcCategory":"","disclosureIndexList":[]}
        return urllib.request.Request(self.URL,data=json.dumps(criteria).encode(),method="POST",
            headers={"Content-Type":"application/json","User-Agent":"BISTBOT/1.0","Referer":"https://www.kap.org.tr/tr/bildirim-sorgu"})

    def _persist_success(self,items: list[dict],now: datetime,latest: datetime,lease_id: str) -> int:
        cutoff=(now-timedelta(days=self.retention_days)).isoformat()
        if self.database is None:
            for item in items:self._memory_raw[self._disclosure_id(item)]=item
            self._memory_raw={key:item for key,item in self._memory_raw.items() if _published(item)>=_parse_time(cutoff)}
            self._memory_state={"last_success_at":now.isoformat(),"cursor_published_at":latest.isoformat(),
                "cooldown_until":None,"consecutive_failures":0,"updated_at":now.isoformat(),
                "lease_owner":None,"lease_expires_at":None}
            return 0
        db=self.database.connection
        with db:
            owner=db.execute("SELECT lease_owner FROM kap_disclosure_poll_state WHERE singleton=1").fetchone()
            if not owner or owner[0]!=lease_id: raise KapLeaseLost("KAP poll lease expired before persistence")
            for item in items:
                db.execute("INSERT OR IGNORE INTO kap_disclosures_raw(disclosure_id,published_at,fetched_at,payload) VALUES(?,?,?,?)",
                    (self._disclosure_id(item),_published(item).isoformat(),now.isoformat(),json.dumps(item,ensure_ascii=False)))
            deleted=0
            for sql,args in (
                ("DELETE FROM kap_disclosures_raw WHERE published_at < ?",(cutoff,)),
                ("DELETE FROM kap_items WHERE timestamp < ?",(cutoff,)),
                ("DELETE FROM event_items WHERE source_type='KAP' AND published_at < ?",(cutoff,)),
                ("DELETE FROM kap_processed_catalysts WHERE published_at < ?",(cutoff,))):
                deleted+=db.execute(sql,args).rowcount
            db.execute("INSERT INTO kap_disclosure_poll_state(singleton,last_success_at,cursor_published_at,cooldown_until,consecutive_failures,last_http_status,last_error,updated_at,lease_owner,lease_expires_at) VALUES(1,?,?,NULL,0,200,NULL,?,NULL,NULL) ON CONFLICT(singleton) DO UPDATE SET last_success_at=excluded.last_success_at,cursor_published_at=excluded.cursor_published_at,cooldown_until=NULL,consecutive_failures=0,last_http_status=200,last_error=NULL,updated_at=excluded.updated_at,lease_owner=NULL,lease_expires_at=NULL",
                (now.isoformat(),latest.isoformat(),now.isoformat()))
        return deleted

    def _cached_events(self,symbols: Sequence[str],now: datetime) -> list[EventItem]:
        from bistbot.intelligence.ranking import make_event
        wanted={symbol.removesuffix(".IS") for symbol in symbols}; cutoff=now-timedelta(days=self.lookback_days)
        if self.database is None: items=list(self._memory_raw.values())
        else:
            rows=self.database.query("SELECT payload FROM kap_disclosures_raw WHERE published_at>=? ORDER BY published_at",(cutoff.isoformat(),))
            items=[json.loads(row["payload"]) for row in rows]
        events=[]
        for item in items:
            published=_published(item); source_id=self._disclosure_id(item)
            codes=_stock_codes(item.get("stockCodes"))|_stock_codes(item.get("relatedStocks"))
            title=str(item.get("summary") or item.get("subject") or "KAP disclosure").strip()
            for code in sorted(codes&wanted):
                events.append(make_event(symbol=f"{code}.IS",source="KAP",source_type=EventSourceType.KAP,
                    title=title,body=str(item.get("kapTitle") or ""),published_at=published,fetched_at=now,
                    url=f"https://www.kap.org.tr/tr/Bildirim/{source_id}",trust_score=95,
                    source_id=f"kap:{source_id}:{code}"))
        return list({event.id:event for event in events}.values())

    def _rate_limit(self,now,headers,state,lease_id):
        failures=int(state.get("consecutive_failures") or 0)+1
        retry_after=_retry_after(headers,now)
        delay=(retry_after if retry_after is not None else min(self.max_backoff_seconds,
            min(self.max_backoff_seconds,self.base_backoff_seconds*2**(failures-1))+
            self.jitter(0,self.base_backoff_seconds)))
        until=now+timedelta(seconds=delay); self._write_state(now,until,failures,429,"KAP HTTP 429",lease_id)
        return until

    def _record_failure(self,now,error,state,lease_id):
        failures=int(state.get("consecutive_failures") or 0)+1
        self._write_state(now,None,failures,getattr(error,"code",None),str(error)[:200],lease_id)

    def _write_state(self,now,cooldown,failures,status,error,lease_id):
        values={"cooldown_until":cooldown.isoformat() if cooldown else None,"consecutive_failures":failures,
            "last_http_status":status,"last_error":error,"updated_at":now.isoformat()}
        if self.database is None:
            self._memory_state={**self._memory_state,**values,"lease_owner":None,"lease_expires_at":None};return
        with self.database.connection:
            self.database.connection.execute("UPDATE kap_disclosure_poll_state SET cooldown_until=?,consecutive_failures=?,last_http_status=?,last_error=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE singleton=1 AND lease_owner=?",
                (values["cooldown_until"],failures,status,error,values["updated_at"],lease_id))

    def _claim_lease(self,now):
        lease_id=uuid4().hex
        lease_seconds=max(60,self.timeout_seconds*self.max_attempts+self.max_backoff_seconds*self.max_attempts+30)
        expires=now+timedelta(seconds=lease_seconds)
        if self.database is None:
            state=self._memory_state; cooldown=_parse_time(state.get("cooldown_until")); active=_parse_time(state.get("lease_expires_at"))
            if cooldown and now<cooldown:return None,state,"KAP_COOLDOWN"
            if active and now<active:return None,state,"KAP_POLL_IN_PROGRESS"
            self._memory_state={**state,"lease_owner":lease_id,"lease_expires_at":expires.isoformat(),"updated_at":now.isoformat()}
            return lease_id,self._memory_state,None
        db=self.database.connection
        db.execute("BEGIN IMMEDIATE")
        try:
            row=db.execute("SELECT * FROM kap_disclosure_poll_state WHERE singleton=1").fetchone()
            state=dict(row) if row else {}; cooldown=_parse_time(state.get("cooldown_until")); active=_parse_time(state.get("lease_expires_at"))
            if cooldown and now<cooldown:
                db.commit(); return None,state,"KAP_COOLDOWN"
            if active and now<active:
                db.commit(); return None,state,"KAP_POLL_IN_PROGRESS"
            db.execute("INSERT INTO kap_disclosure_poll_state(singleton,consecutive_failures,updated_at,lease_owner,lease_expires_at) VALUES(1,0,?,?,?) ON CONFLICT(singleton) DO UPDATE SET lease_owner=excluded.lease_owner,lease_expires_at=excluded.lease_expires_at,updated_at=excluded.updated_at",
                (now.isoformat(),lease_id,expires.isoformat()))
            db.commit(); state={**state,"lease_owner":lease_id,"lease_expires_at":expires.isoformat()}
            return lease_id,state,None
        except Exception:
            db.rollback(); raise

    def _state(self):
        if self.database is None:return self._memory_state
        rows=self.database.query("SELECT * FROM kap_disclosure_poll_state WHERE singleton=1")
        return dict(rows[0]) if rows else {}

    def _retry_delay(self,attempt):
        base=min(self.max_backoff_seconds,self.base_backoff_seconds*2**(attempt-1))
        return min(self.max_backoff_seconds,base+self.jitter(0,min(base,self.base_backoff_seconds)))

    @staticmethod
    def _disclosure_id(item): return str(item.get("disclosureIndex") or item.get("id") or "")

    def _diagnostics(self,now,state,events,**updates):
        return IntelligenceProviderDiagnostics(provider=type(self).__name__,mode=self.provider_mode,endpoint=self.URL,
            request_timestamp=now,status=state,mapped_events=len(events),symbols_matched=len({e.symbol for e in events}),
            latest_event=max((e.published_at for e in events),default=None),cached_events=len(events),**updates)


def _published(item):
    value=str(item["publishDate"])
    try:return datetime.strptime(value,"%d.%m.%Y %H:%M:%S").replace(tzinfo=ZoneInfo("Europe/Istanbul"))
    except ValueError:return _aware(datetime.fromisoformat(value))

def _parse_time(value): return _aware(datetime.fromisoformat(value)) if value else None
def _aware(value): return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

def _retry_after(headers,now):
    value=headers.get("Retry-After") if hasattr(headers,"get") else None
    if not value:return None
    try:return max(0,float(value))
    except ValueError:
        try:return max(0,(parsedate_to_datetime(value)-now).total_seconds())
        except (TypeError,ValueError,OverflowError):return None

def _stock_codes(value) -> set[str]:
    parts=value if isinstance(value,list) else re.split(r"[,;/ ]+",str(value or ""))
    return {str(part).strip().upper() for part in parts if re.fullmatch(r"[A-Z0-9]{3,6}",str(part).strip().upper())}
