from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hashlib
import json
from email.utils import parsedate_to_datetime
import sqlite3
import threading
import time
import random
from typing import Any
import urllib.parse
import urllib.request
import urllib.error
from html.parser import HTMLParser
import re
import unicodedata
from zoneinfo import ZoneInfo
from statistics import median

from .models import FinancialPeriod, FundamentalProviderStatus, FundamentalSnapshot, Metric


class _SharedRateLimiter:
    def __init__(self,minimum,maximum,sleeper,clock,jitter):
        self.minimum=minimum;self.maximum=maximum;self.sleeper=sleeper;self.clock=clock;self.jitter=jitter
        self.lock=threading.Lock();self.next_allowed=0.0
        self.request_lock=threading.Lock()
    def wait(self):
        # Keep the reservation and request start atomic across provider instances.
        with self.lock:
            delay=max(0.0,self.next_allowed-self.clock())
            if delay:self.sleeper(delay)
            self.next_allowed=self.clock()+self.jitter(self.minimum,self.maximum)
    def defer(self,delay):
        # Retry-After is a server-wide cooldown, not a per-symbol delay.
        with self.lock:
            delay=max(0.0,delay)
            self.next_allowed=max(self.next_allowed,self.clock()+delay)


_LIMITERS_LOCK=threading.Lock()
_LIMITERS: dict[tuple[Any,...],_SharedRateLimiter]={}
def _shared_limiter(opener,sleeper,clock,minimum,maximum,jitter):
    key=(opener,sleeper,clock,float(minimum),float(maximum),jitter)
    with _LIMITERS_LOCK:
        return _LIMITERS.setdefault(key,_SharedRateLimiter(minimum,maximum,sleeper,clock,jitter))


class FundamentalDataProvider(ABC):
    """Provider boundary. Strategy code consumes only normalized snapshots."""
    @abstractmethod
    def get_snapshot(self, symbol: str, as_of: datetime) -> FundamentalSnapshot: ...


class UnavailableFundamentalProvider(FundamentalDataProvider):
    def get_snapshot(self, symbol: str, as_of: datetime) -> FundamentalSnapshot:
        return FundamentalSnapshot(symbol=symbol, as_of=as_of, source="none",
            provider_status=FundamentalProviderStatus.UNAVAILABLE)


class KapFundamentalProvider(FundamentalDataProvider):
    """Official KAP financial-report adapter with a durable, revision-aware cache.

    Network and sleep functions are injectable so tests never contact KAP.  KAP's
    response has changed shape over time; parsing therefore uses semantic account
    codes and accepts both the current ``financials`` envelope and XBRL-like facts.
    """
    MEMBER_URL = "https://www.kap.org.tr/tr/api/member/filter/{ticker}"
    MEMBER_CATALOG_URL = "https://www.kap.org.tr/tr/api/company/items/IGS/A"
    DISCLOSURE_URL = "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
    DETAIL_URL = "https://www.kap.org.tr/tr/Bildirim/{filing_id}"
    SOURCE = "KAP (Kamuyu Aydinlatma Platformu)"

    def __init__(self, database_path: str, *, opener=urllib.request.urlopen,
                 timeout_seconds: float = 12, max_attempts: int = 3,
                 refresh_interval: timedelta = timedelta(hours=6),
                 failure_backoff: timedelta = timedelta(minutes=10),
                 max_cache_age: timedelta = timedelta(days=45),
                 min_request_interval: float = 1.5, max_request_interval: float | None = None,
                 sleeper=time.sleep, jitter: Callable[[float,float],float] = random.uniform,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 monotonic: Callable[[], float] = time.monotonic,
                 price_resolver: Callable[[str], float | None] | None = None):
        self.database_path=database_path; self.opener=opener; self.timeout_seconds=timeout_seconds
        self.max_attempts=max_attempts; self.refresh_interval=refresh_interval
        self.failure_backoff=failure_backoff
        self.max_cache_age=max_cache_age
        max_request_interval=(2.5 if min_request_interval==1.5 else min_request_interval) if max_request_interval is None else max_request_interval
        if max_request_interval < min_request_interval:raise ValueError("max request interval must be >= minimum")
        self.min_request_interval=min_request_interval; self.max_request_interval=max_request_interval; self.sleeper=sleeper; self.now=now
        self.monotonic=monotonic
        self.price_resolver=price_resolver or self._cached_market_price
        # The limiter is deliberately shared by all live provider instances. Its
        # dependency-based scope keeps separately injected test clients isolated.
        self._rate_limiter=_shared_limiter(opener,sleeper,monotonic,min_request_interval,max_request_interval,jitter)
        self._member_catalog: list[dict[str,Any]]|None=None

    def get_snapshot(self, symbol: str, as_of: datetime) -> FundamentalSnapshot:
        ticker=symbol.upper().removesuffix(".IS"); error=None; refreshed=False; source_url=None
        checked_at=self.now()
        if checked_at.tzinfo is None:checked_at=checked_at.replace(tzinfo=timezone.utc)
        else:checked_at=checked_at.astimezone(timezone.utc)
        try:
            if self._refresh_due(ticker, checked_at):
                oid=self._member_identity(ticker,checked_at)
                cached_before=self._load(ticker,as_of);bootstrap=len(cached_before)<8
                range_start=as_of.date()-timedelta(days=1100 if bootstrap else 120);range_end=as_of.date();filings=[]
                windows=list(_calendar_year_windows(range_start,range_end))
                for window_start,window_end in reversed(windows):
                    filings.extend(self._discover(oid,window_start,window_end))
                    # A current financial filing carries comparative columns and is
                    # enough for a safe PARTIAL cold start. Older years are populated
                    # by later refreshes instead of burst-fetching four years at once.
                    if bootstrap and any(isinstance(f,dict) and str(f.get("disclosureClass"))=="FR"
                        and ticker in _stock_codes(f.get("stockCodes")) and _is_financial_statement_filing(f)
                        for f in filings):break
                filings=list({str(f.get("disclosureIndex")):f for f in filings if isinstance(f,dict) and f.get("disclosureIndex")}.values())
                filings=[f for f in filings if isinstance(f,dict) and str(f.get("disclosureClass"))=="FR"
                    and ticker in _stock_codes(f.get("stockCodes")) and _is_financial_statement_filing(f)
                    and (published := _date(f.get("publishDate"))) is not None
                    and published <= _utc(as_of)]
                filings.sort(key=lambda f:_date(f.get("publishDate")) or datetime.min.replace(tzinfo=timezone.utc),reverse=True)
                known=self._filing_ids(ticker); batches=[]
                for filing in filings:
                    filing_id=str(filing.get("disclosureIndex") or "")
                    if not filing_id or filing_id in known:continue
                    source_url=self.DETAIL_URL.format(filing_id=filing_id)
                    html=self._request(source_url,json_response=False)
                    published=_date(filing.get("publishDate"))
                    parsed=self._parse_html(html,filing_id=filing_id,published_at=published,quarterize=False)
                    if not parsed:raise ValueError(f"KAP filing {filing_id} contains no valid financial periods")
                    batches.append((filing_id,published,source_url,parsed))
                if batches:
                    self._commit_filings(ticker,batches,checked_at)
                elif not filings:
                    if cached_before:self._record_success(ticker,checked_at,self._latest_filing_id(ticker))
                    else:raise ValueError("KAP response contains no financial-report disclosures")
                else:self._record_success(ticker,checked_at,str(filings[0].get("disclosureIndex") or ""))
                refreshed=True
        except Exception as exc:  # cache remains a safe technical-only/partial fallback
            error=f"{type(exc).__name__}: {str(exc)[:180]}"
            self._record_check(ticker,checked_at,error=error)
        periods=self._load(ticker,as_of)
        shares=self._member_shares(ticker)
        if periods and shares is not None:periods[0].outstanding_shares=_metric_value(shares,"KAP:validated_paid_capital/nominal_value")
        cache_audit=self._cache_audit(ticker)
        source_url=cache_audit.get("source_url") or source_url
        self._apply_valuation(symbol,periods,as_of)
        essentials=("revenue","net_income","equity","total_assets")
        useful=bool(periods and sum(getattr(periods[0],name).available for name in essentials)>=3)
        complete_history=len(periods)>=8 and all(p.revenue.available and p.net_income.available for p in periods[:8])
        reference=as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        latest_end=(periods[0].period_end if periods and periods[0].period_end.tzinfo else
                    periods[0].period_end.replace(tzinfo=timezone.utc) if periods else None)
        implausibly_old=self._report_stale(periods,reference)
        stale=implausibly_old or bool(error and self._cache_stale(ticker,checked_at))
        required=("revenue","gross_profit","operating_profit","ebitda","net_income","operating_cash_flow",
            "investing_cash_flow","capex","free_cash_flow","cash_and_equivalents","short_term_financial_debt",
            "long_term_financial_debt","total_debt","net_debt","total_assets","total_liabilities","equity")
        materially_complete=complete_history and all(all(getattr(p,name).available for name in required) for p in periods[:8])
        status=(FundamentalProviderStatus.UNAVAILABLE if not useful or stale else
                FundamentalProviderStatus.AVAILABLE if materially_complete else FundamentalProviderStatus.PARTIAL)
        company_type,company_type_status,company_type_source=self._member_company_profile(ticker)
        return FundamentalSnapshot(symbol=symbol,as_of=as_of,source=self.SOURCE,
            provider_status=status,periods=periods[:12],audit_metadata={
                "official_source":"https://www.kap.org.tr", "cache":"sqlite",
                "refreshed":refreshed,"refresh_error":error,"period_count":len(periods),
                "source_url":source_url,"fetched_at":cache_audit.get("fetched_at"),
                "discovery_checked_at":checked_at.isoformat() if refreshed else cache_audit.get("discovery_checked_at"),
                "latest_period_end":periods[0].period_end.isoformat() if periods else None,
                "currency":periods[0].currency if periods else None,"unit":periods[0].unit if periods else None,
                "consolidation_scope":periods[0].consolidated if periods else None,
                "consolidation_policy":"prefer_consolidated","missing_values":"None",
                "company_type":company_type,"company_type_status":company_type_status,
                "company_type_source":company_type_source})

    def _refresh_due(self,ticker: str,as_of: datetime) -> bool:
        with sqlite3.connect(self.database_path) as db:
            row=db.execute("SELECT last_checked_at,error FROM kap_fundamental_refresh WHERE symbol=?",(ticker,)).fetchone()
        if not row:return True
        checked=datetime.fromisoformat(row[0]); reference=as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        if checked.tzinfo is None: checked=checked.replace(tzinfo=timezone.utc)
        return reference-checked >= (self.failure_backoff if row[1] else self.refresh_interval)

    def _json(self,url: str) -> Any:
        return self._request(url,json_response=True)

    def _request(self,url: str,*,json_response: bool,post_data: dict|None=None) -> Any:
        body=json.dumps(post_data).encode() if post_data is not None else None
        request=urllib.request.Request(url,data=body,method="POST" if body else "GET",headers={"Accept":"application/json,text/html","Content-Type":"application/json","User-Agent":"BISTBOT/1.0 (+official KAP fundamentals)","Referer":"https://www.kap.org.tr/tr/bildirim-sorgu"})
        for attempt in range(self.max_attempts):
            with self._rate_limiter.request_lock:
                try:
                    self._rate_limiter.wait()
                    with self.opener(request,timeout=self.timeout_seconds) as response:
                        raw=response.read(); status=getattr(response,"status",200)
                        retry_after=_retry_after(getattr(response,"headers",None),now=self.now)
                    if status != 200: raise _KapHttpError(status,retry_after)
                    if len(raw)>20_000_000: raise ValueError("KAP response exceeds safety limit")
                    decoded=raw.decode("utf-8")
                    return json.loads(decoded) if json_response else decoded
                except Exception as exc:
                    retry_after=_retry_after(getattr(exc,"headers",None),now=self.now)
                    if isinstance(exc,_KapHttpError):retry_after=exc.retry_after
                    delay=retry_after if retry_after is not None else min(.5*2**attempt,8)
                    # Register the server-wide cooldown before another serialized
                    # request can acquire the request lock.
                    self._rate_limiter.defer(delay)
                    if attempt+1>=self.max_attempts:raise

    def _discover(self,oid,start,end):
        criteria={"fromDate":start.isoformat(),"toDate":end.isoformat(),"memberType":"IGS",
            "mkkMemberOidList":[oid],"inactiveMkkMemberOidList":[],"disclosureClass":"","subjectList":[],
            "isLate":"","mainSector":"","sector":"","subSector":"","marketOid":"","index":"",
            "bdkReview":"","bdkMemberOidList":[],"year":"","term":"","ruleType":"","period":"",
            "fromSrc":False,"srcCategory":"","disclosureIndexList":[]}
        page=self._request(self.DISCLOSURE_URL,json_response=True,post_data=criteria)
        if not isinstance(page,list):raise ValueError("KAP disclosure response is not a list")
        if len(page)>=250 and start<end:
            midpoint=start+timedelta(days=(end-start).days//2)
            return self._discover(oid,start,midpoint)+self._discover(oid,midpoint+timedelta(days=1),end)
        if len(page)>=250:raise ValueError("KAP disclosure response limit reached for one-day window")
        return page

    def _parse_html(self,html: str,*,filing_id: str="page",published_at: datetime|None=None,quarterize: bool=True) -> list[FinancialPeriod]:
        parser=_TableParser(); parser.feed(html)
        text=" ".join(parser.text)
        presentation_match=re.search(r"(?:Sunum Para Birimi|Presentation Currency)\s*:?\s*((?:1[.,]?000[.,]?000|1[.,]?000|Bin|Milyon|Thousand|Million)?\s*(?:TL|TRY))",text,re.I)
        unit_match=re.search(r"(?:Birim|Unit)\s*:?\s*(TL|TRY|Bin TL|Thousand TRY|Milyon TL|Million TRY|1000 TL|1000000 TL)",text,re.I)
        if not presentation_match:return []
        global_currency,unit=_presentation(presentation_match.group(1))
        if unit_match:unit=_unit(unit_match.group(1))
        if global_currency is None or unit is None:return []
        consolidated=(False if re.search(r"Konsolide Olmayan|Unconsolidated",text,re.I)
            else True if re.search(r"Konsolide|Consolidated",text,re.I) else None)
        tables=[table for table in parser.tables if table and any(len(row)>1 for row in table)]
        columns: dict[str,dict[str,float|None]]={}; column_meta: dict[str,dict[str,str]]={}
        for table in tables:
            header_index=next((i for i,row in enumerate(table) if sum(_period_end(cell) is not None for cell in row)>=1),None)
            if header_index is None:continue
            headers=table[header_index]
            period_indexes=[index for index,header in enumerate(headers) if _period_end(header)]
            first_period=min(period_indexes)
            for index in period_indexes:
                header=headers[index];columns.setdefault(header,{});column_meta.setdefault(header,{})
            for row in table[header_index+1:]:
                if not row:continue
                labels=[cell for cell in row[:first_period] if cell]
                label=next((_fold_label(cell) for cell in reversed(labels)
                    if _fold_label(cell) in {"sunum para birimi","presentation currency","finansal tablo niteligi","nature of financial statements"}),"")
                if label in {"sunum para birimi","presentation currency","finansal tablo niteligi","nature of financial statements"}:
                    key="currency" if "para birimi" in label or "currency" in label else "consolidation"
                    for index,header in enumerate(headers):
                        if header in columns and index<len(row):column_meta[header][key]=row[index]
                    continue
                # Hierarchy columns run broad-to-specific; select the leaf account.
                alias=next((_account_alias(cell) for cell in reversed(labels) if _account_alias(cell)),None)
                if not alias:continue
                for index,header in enumerate(headers):
                    if header in columns and index<len(row):columns[header][alias]=_number(row[index])
        fragments: list[dict[str,Any]]=[]
        for header,facts in columns.items():
            end=_period_end(header)
            if end is None:continue
            meta=column_meta.get(header,{})
            raw_presentation=meta.get("currency") or presentation_match.group(1)
            column_currency,column_unit=_presentation(raw_presentation)
            if unit_match and str(raw_presentation).strip().upper() in {"TL","TRY"}:column_unit=unit
            if column_currency is None or column_unit is None:continue
            nature=str(meta.get("consolidation") or "")
            column_consolidated=(False if re.search(r"Konsolide Olmayan|Unconsolidated",nature,re.I) else
                True if re.search(r"Konsolide|Consolidated",nature,re.I) else consolidated)
            range_match=re.search(r"(\d{2}[./]\d{2}[./]\d{4})\s*[-–]\s*(\d{2}[./]\d{2}[./]\d{4})",header)
            start=_date(range_match.group(1)) if range_match else (datetime(end.year,1,1,tzinfo=end.tzinfo) if re.fullmatch(r"\s*\d{4}/(?:3|6|9|12)\s*",header) else None)
            fragments.append({"filingId":filing_id,"publishedAt":published_at,"currency":"TRY","unit":column_unit,
                "consolidated":column_consolidated,"periodStart":start,"periodEnd":end.isoformat(),"facts":facts}
                )
        records_by_period: dict[tuple[Any,...],dict[str,Any]]={}
        flow_names={"revenue","gross_profit","operating_profit","ebitda","net_income","net_interest_income","operating_cash_flow","investing_cash_flow","capex","free_cash_flow"}
        for fragment in fragments:
            period_key=(fragment["periodEnd"],fragment["consolidated"],fragment["currency"],fragment["unit"])
            merged=records_by_period.setdefault(period_key,{**fragment,"facts":{}})
            if fragment["periodStart"]:merged["periodStart"]=fragment["periodStart"]
            for name,value in fragment["facts"].items():
                # Range columns own flows; instant columns own balance-sheet facts.
                if name not in merged["facts"] or (fragment["periodStart"] and name in flow_names):merged["facts"][name]=value
        records=list(records_by_period.values())
        return self._parse_periods({"financials":records},quarterize=quarterize)

    def _filing_ids(self,ticker):
        with sqlite3.connect(self.database_path) as db:return {row[0] for row in db.execute("SELECT filing_id FROM kap_processed_disclosures WHERE symbol=?",(ticker,))}
    def _member_identity(self,ticker,checked):
        with sqlite3.connect(self.database_path) as db:
            row=db.execute("SELECT mkk_member_oid,resolved_at,company_type FROM kap_member_cache WHERE symbol=?",(ticker,)).fetchone()
        if row:
            resolved=datetime.fromisoformat(row[1]);reference=checked if checked.tzinfo else checked.replace(tzinfo=timezone.utc)
            if resolved.tzinfo is None:resolved=resolved.replace(tzinfo=timezone.utc)
            if reference-resolved<timedelta(days=30) and row[2] in {"GENERAL","BANK"}:return row[0]
        payload=self._json(self.MEMBER_URL.format(ticker=urllib.parse.quote(ticker)))
        oid=_member_oid(payload,ticker)
        candidates=_member_candidates(payload)
        if not oid:
            if self._member_catalog is None:
                catalog=self._json(self.MEMBER_CATALOG_URL)
                self._member_catalog=_member_candidates(catalog)
            candidates=[item for item in self._member_catalog
                if ticker in _stock_codes(_first(item,"stockCode","stockCodes","ticker","symbol"))]
            oid=_member_oid(candidates,ticker)
        if not oid:raise ValueError("KAP member response has no unique member OID")
        candidate=next((item for item in candidates if str(_first(item,"mkkMemberOid","memberOid","kapMemberOid","oid"))==oid),{})
        with sqlite3.connect(self.database_path) as db:
            db.execute("PRAGMA busy_timeout=5000")
            shares=_validated_shares(candidate)
            company_type=_company_type(candidate)
            db.execute("INSERT OR REPLACE INTO kap_member_cache(symbol,mkk_member_oid,company_code,permalink,resolved_at,outstanding_shares,company_type) VALUES(?,?,?,?,?,?,?)",
                (ticker,oid,_first(candidate,"companyCode"),_first(candidate,"permaLink"),checked.isoformat(),shares,company_type))
        return oid

    def _member_company_type(self,ticker):
        return self._member_company_profile(ticker)[0]

    def _member_company_profile(self,ticker):
        with sqlite3.connect(self.database_path) as db:
            row=db.execute("SELECT company_type,company_code,permalink FROM kap_member_cache WHERE symbol=?",(ticker,)).fetchone()
        if row and row[0] in {"GENERAL","BANK"}:return str(row[0]),"CONFIRMED","KAP_MEMBER_CACHE"
        if row:
            inferred=_company_type({"companyCode":row[1],"permalink":row[2]})
            if inferred=="BANK":return inferred,"INFERRED","KAP_PERMALINK"
        return "GENERAL","PROVISIONAL","DEFAULT"

    def _member_shares(self,ticker):
        with sqlite3.connect(self.database_path) as db:row=db.execute("SELECT outstanding_shares FROM kap_member_cache WHERE symbol=?",(ticker,)).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def _commit_filings(self,ticker,batches,checked):
        """Atomically persist raw filings, their ledger, and rebuilt normalized quarters."""
        with sqlite3.connect(self.database_path) as db:
            db.execute("PRAGMA busy_timeout=5000");db.execute("BEGIN IMMEDIATE")
            try:
                for filing_id,published,url,periods in batches:
                    if published is None:raise ValueError(f"KAP filing {filing_id} has no publication timestamp")
                    for period in periods:
                        db.execute("INSERT OR REPLACE INTO kap_raw_filing_periods(symbol,filing_id,period_end,consolidated,published_at,payload) VALUES(?,?,?,?,?,?)",
                            (ticker,filing_id,period.period_end.isoformat(),int(bool(period.consolidated)),published.isoformat(),period.model_dump_json()))
                    db.execute("INSERT INTO kap_processed_disclosures(symbol,filing_id,published_at,processed_at,status,source_url) VALUES(?,?,?,?,?,?)",
                        (ticker,filing_id,published.isoformat(),checked.isoformat(),"PARSED",url))
                rows=db.execute("SELECT payload FROM kap_raw_filing_periods WHERE symbol=? ORDER BY published_at DESC",(ticker,)).fetchall()
                raw=[FinancialPeriod.model_validate_json(row[0]) for row in rows]
                normalized=_normalize_revisions(raw)[:12]
                db.execute("DELETE FROM kap_financial_cache WHERE symbol=?",(ticker,))
                for period in normalized:self._insert_cache(db,ticker,period,checked)
                latest=max(batches,key=lambda item:item[1])[0]
                db.execute("INSERT INTO kap_fundamental_refresh(symbol,last_checked_at,latest_filing_id,last_success_at,error) VALUES(?,?,?,?,NULL) ON CONFLICT(symbol) DO UPDATE SET last_checked_at=excluded.last_checked_at,latest_filing_id=excluded.latest_filing_id,last_success_at=excluded.last_success_at,error=NULL",
                    (ticker,checked.isoformat(),latest,checked.isoformat()))
                db.commit()
            except Exception:
                db.rollback();raise

    def _insert_cache(self,db,ticker,p,checked):
        raw=p.model_dump_json();canonical={"period_end":p.period_end.isoformat(),"currency":p.currency,"unit":p.unit,
            "consolidated":p.consolidated,"values":{name:getattr(p,name).value for name in _ALIASES}}
        digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        db.execute("INSERT INTO kap_financial_cache(symbol,period_end,consolidated,filing_id,published_at,content_hash,payload,fetched_at) VALUES(?,?,?,?,?,?,?,?)",
            (ticker,p.period_end.isoformat(),int(bool(p.consolidated)),p.filing_id,p.published_at.isoformat() if p.published_at else None,digest,raw,checked.isoformat()))
    def _cache_stale(self,ticker,as_of):
        with sqlite3.connect(self.database_path) as db:row=db.execute("SELECT last_success_at FROM kap_fundamental_refresh WHERE symbol=?",(ticker,)).fetchone()
        if not row or not row[0]:return True
        value=datetime.fromisoformat(row[0]); reference=as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
        return reference-value>self.max_cache_age
    @staticmethod
    def _report_stale(periods,reference):
        if not periods:return False
        ends=sorted({p.period_end for p in periods},reverse=True)
        intervals=[(ends[index]-ends[index+1]).days for index in range(len(ends)-1) if (ends[index]-ends[index+1]).days>0]
        cadence=median(intervals[:6]) if intervals else 90
        tolerance=max(220,min(550,cadence*2.5))
        latest=ends[0] if ends[0].tzinfo else ends[0].replace(tzinfo=timezone.utc)
        return (reference-latest).days>tolerance

    def _parse_periods(self,payload: Any,*,quarterize: bool=True) -> list[FinancialPeriod]:
        records=payload
        if isinstance(payload,dict):
            records=next((payload[key] for key in ("financials","periods","data") if key in payload),None)
        if isinstance(records,dict): records=records.get("items") or records.get("rows") or []
        if not isinstance(records,list): raise ValueError("invalid KAP financial payload")
        parsed=[]
        for record in records:
            if not isinstance(record,dict):continue
            end=_date(_first(record,"periodEnd","period_end","endDate","date"))
            if not end:continue
            consolidated=_bool(_first(record,"consolidated","isConsolidated","consolidation"))
            unit=_unit(_first(record,"unit","scale","currencyUnit")); raw_currency=_first(record,"currency","currencyCode")
            currency=str(raw_currency).upper() if raw_currency is not None else None
            if currency not in {"TRY","TRL"} or unit is None: continue
            facts=record.get("facts") or record.get("items") or record.get("values") or record
            values=_facts(facts,unit)
            kwargs={name:_metric(values.get(name),record) for name in _ALIASES}
            if kwargs["capex"].value is not None:
                kwargs["capex"]=_metric_value(abs(kwargs["capex"].value),kwargs["capex"].source or "KAP")
            # Derivations require every accounting input; absence stays unknown.
            if kwargs["total_debt"].value is None:
                kwargs["total_debt"]=_sum_metrics(kwargs["short_term_financial_debt"],kwargs["long_term_financial_debt"],"derived:debt")
            if kwargs["net_debt"].value is None:
                kwargs["net_debt"]=_subtract(kwargs["total_debt"],kwargs["cash_and_equivalents"],"derived:net_debt")
            if kwargs["free_cash_flow"].value is None:
                kwargs["free_cash_flow"]=_subtract(kwargs["operating_cash_flow"],kwargs["capex"],"derived:fcf")
            for target,numerator in (("gross_margin","gross_profit"),("operating_margin","operating_profit"),("ebitda_margin","ebitda"),("net_margin","net_income")):
                if kwargs[target].value is None: kwargs[target]=_ratio(kwargs[numerator],kwargs["revenue"],f"derived:{target}")
            filing=str(_first(record,"filingId","disclosureIndex","id") or f"{end.date()}:{int(bool(consolidated))}")
            parsed.append(FinancialPeriod(period_end=end,period_start=_date(_first(record,"periodStart","startDate")),
                currency="TRY",unit=unit,consolidated=consolidated,filing_id=filing,
                published_at=_date(_first(record,"publishedAt","publishDate")),**kwargs))
        # One statement per period: consolidated is preferred, newest publication wins.
        parsed.sort(key=lambda p:(p.period_end,bool(p.consolidated),p.published_at or p.period_end),reverse=True)
        selected: dict[Any,FinancialPeriod]={}
        for period in parsed:selected.setdefault(period.period_end.date(),period)
        values=list(selected.values())
        return (_quarterize(values) if quarterize else sorted(values,key=lambda p:p.period_end,reverse=True))[:12]

    def _store(self,ticker: str,periods: list[FinancialPeriod],checked: datetime) -> None:
        latest=None
        with sqlite3.connect(self.database_path) as db:
            db.execute("PRAGMA busy_timeout=5000")
            for p in periods:
                existing=db.execute("SELECT consolidated,published_at FROM kap_financial_cache WHERE symbol=? AND period_end=? ORDER BY consolidated DESC,published_at DESC LIMIT 1",(ticker,p.period_end.isoformat())).fetchone()
                incoming_published=p.published_at.isoformat() if p.published_at else None
                if existing and ((existing[1] and (not incoming_published or existing[1]>incoming_published)) or
                                 (existing[1]==incoming_published and existing[0] and not p.consolidated)):
                    continue
                raw=p.model_dump_json(); canonical={"period_end":p.period_end.isoformat(),"currency":p.currency,
                    "unit":p.unit,"consolidated":p.consolidated,
                    "values":{name:getattr(p,name).value for name in _ALIASES}}
                digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":")).encode()).hexdigest()
                latest=latest or p.filing_id
                db.execute("INSERT INTO kap_financial_cache(symbol,period_end,consolidated,filing_id,published_at,content_hash,payload,fetched_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(symbol,period_end,consolidated) DO UPDATE SET filing_id=excluded.filing_id,published_at=excluded.published_at,content_hash=excluded.content_hash,payload=excluded.payload,fetched_at=excluded.fetched_at WHERE kap_financial_cache.content_hash<>excluded.content_hash AND (kap_financial_cache.published_at IS NULL OR (excluded.published_at IS NOT NULL AND excluded.published_at>=kap_financial_cache.published_at))",
                    (ticker,p.period_end.isoformat(),int(bool(p.consolidated)),p.filing_id,incoming_published,digest,raw,checked.isoformat()))
                db.execute("DELETE FROM kap_financial_cache WHERE symbol=? AND period_end=? AND consolidated<>?",(ticker,p.period_end.isoformat(),int(bool(p.consolidated))))
            db.execute("INSERT INTO kap_fundamental_refresh(symbol,last_checked_at,latest_filing_id,last_success_at,error) VALUES(?,?,?,?,NULL) ON CONFLICT(symbol) DO UPDATE SET last_checked_at=excluded.last_checked_at,latest_filing_id=excluded.latest_filing_id,last_success_at=excluded.last_success_at,error=NULL",(ticker,checked.isoformat(),latest,checked.isoformat()))

    def _record_check(self,ticker,checked,error):
        with sqlite3.connect(self.database_path) as db:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("INSERT INTO kap_fundamental_refresh(symbol,last_checked_at,error) VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET last_checked_at=excluded.last_checked_at,error=excluded.error",(ticker,checked.isoformat(),error))
    def _record_success(self,ticker,checked,latest):
        with sqlite3.connect(self.database_path) as db:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("INSERT INTO kap_fundamental_refresh(symbol,last_checked_at,latest_filing_id,last_success_at,error) VALUES(?,?,?,?,NULL) ON CONFLICT(symbol) DO UPDATE SET last_checked_at=excluded.last_checked_at,latest_filing_id=excluded.latest_filing_id,last_success_at=excluded.last_success_at,error=NULL",(ticker,checked.isoformat(),latest,checked.isoformat()))

    def _load(self,ticker: str,as_of: datetime|None=None) -> list[FinancialPeriod]:
        with sqlite3.connect(self.database_path) as db:
            rows=db.execute("SELECT payload FROM kap_financial_cache WHERE symbol=? ORDER BY period_end DESC,consolidated DESC",(ticker,)).fetchall()
            raw_rows=db.execute("SELECT payload FROM kap_raw_filing_periods WHERE symbol=? ORDER BY published_at DESC",(ticker,)).fetchall()
        # Prefer the durable raw-period cache. Later filings often carry a
        # comparative balance-sheet column with no flow facts; coalescing the
        # revisions preserves the original annual filing's flow observations.
        if as_of is not None:
            cutoff=_utc(as_of)
            raw_periods=[FinancialPeriod.model_validate_json(row[0]) for row in raw_rows]
            raw_periods=[period for period in raw_periods
                if period.published_at is None or _utc(period.published_at)<=cutoff]
            periods=(_normalize_revisions(raw_periods) if raw_rows else
                [FinancialPeriod.model_validate_json(row[0]) for row in rows])
            periods=[period for period in periods
                if period.published_at is None or _utc(period.published_at)<=cutoff]
        else:
            periods=(_normalize_revisions([FinancialPeriod.model_validate_json(row[0]) for row in raw_rows])
                if raw_rows else [FinancialPeriod.model_validate_json(row[0]) for row in rows])
        return periods[:12]
    def _cache_audit(self,ticker):
        with sqlite3.connect(self.database_path) as db:
            row=db.execute("SELECT filing_id,fetched_at FROM kap_financial_cache WHERE symbol=? ORDER BY published_at DESC,fetched_at DESC LIMIT 1",(ticker,)).fetchone()
        return ({"source_url":self.DETAIL_URL.format(filing_id=row[0]),"fetched_at":row[1]} if row else {})
    def _latest_filing_id(self,ticker):
        with sqlite3.connect(self.database_path) as db:
            row=db.execute("SELECT filing_id FROM kap_financial_cache WHERE symbol=? ORDER BY published_at DESC LIMIT 1",(ticker,)).fetchone()
        return str(row[0]) if row else ""

    def _cached_market_price(self,symbol: str):
        with sqlite3.connect(self.database_path) as db:
            row=db.execute("SELECT price,timestamp FROM market_snapshots WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",(symbol,)).fetchone()
        return (float(row[0]),datetime.fromisoformat(row[1])) if row else None

    def _apply_valuation(self,symbol: str,periods: list[FinancialPeriod],as_of: datetime):
        if not periods:return
        latest=periods[0]; quote=self.price_resolver(symbol); shares=latest.outstanding_shares.value
        if not isinstance(quote,tuple) or len(quote)!=2:return
        price,price_time=quote
        if not isinstance(price_time,datetime):return
        reference=as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        observed=price_time if price_time.tzinfo else price_time.replace(tzinfo=timezone.utc)
        if observed>reference or reference-observed>timedelta(days=1):return
        if price is None or shares is None or price<=0 or shares<=0:return
        market_cap=price*shares; latest.market_cap=_metric_value(market_cap,"derived:market_price_x_KAP_capital")
        earnings=_ttm(periods,"net_income"); ebitda=_ttm(periods,"ebitda")
        latest.pe=_metric_value(market_cap/earnings,"derived") if earnings and earnings>0 else Metric()
        latest.pb=_metric_value(market_cap/latest.equity.value,"derived") if latest.equity.value and latest.equity.value>0 else Metric()
        if latest.net_debt.value is not None:
            ev=market_cap+latest.net_debt.value; latest.enterprise_value=_metric_value(ev,"derived")
            latest.ev_ebitda=_metric_value(ev/ebitda,"derived") if ebitda and ebitda>0 else Metric()

    @staticmethod
    def _metrics(period): return [getattr(period,name) for name in _ALIASES]


_ALIASES={
 "revenue":("revenue","sales","ifrs-full_Revenue"),"gross_profit":("grossprofit","gross_profit","ifrs-full_GrossProfit"),
 "operating_profit":("operatingprofit","operating_profit","kap-fr_OperatingProfitLoss"),"ebitda":("ebitda",),
 "net_income":("netincome","net_income","profitloss","ifrs-full_ProfitLoss"),
 "net_interest_income":("netinterestincome","net_interest_income","interestincomeexpenseNet","bdk-netinterestincome"),
 "loans":("loans","loansandreceivables","loansandadvancestocustomers","credits"),
 "deposits":("deposits","customerdeposits","depositsfromcustomers"),
 "operating_cash_flow":("operatingcashflow","operating_cash_flow","ifrs-full_CashFlowsFromUsedInOperatingActivities"),
 "investing_cash_flow":("investingcashflow","investing_cash_flow","ifrs-full_CashFlowsFromUsedInInvestingActivities"),
 "capex":("capex","purchaseofpropertyplantandequipment","ifrs-full_PurchaseOfPropertyPlantAndEquipment"),
 "free_cash_flow":("freecashflow","free_cash_flow"),"cash_and_equivalents":("cashandcashequivalents","cash_and_equivalents","ifrs-full_CashAndCashEquivalents"),
 "short_term_financial_debt":("shorttermborrowings","short_term_financial_debt","ifrs-full_CurrentBorrowings"),
 "long_term_financial_debt":("longtermborrowings","long_term_financial_debt","ifrs-full_NoncurrentBorrowings"),
 "total_debt":("totaldebt","total_financial_debt"),"net_debt":("netdebt","net_debt"),
 "total_assets":("totalassets","total_assets","ifrs-full_Assets"),"total_liabilities":("totalliabilities","total_liabilities","ifrs-full_Liabilities"),
 "equity":("equity","totalequity","ifrs-full_Equity"),"outstanding_shares":("outstanding_shares",),
 "market_cap":("marketcap",),"pe":("pe",),"pb":("pb",),"enterprise_value":("enterprisevalue",),"ev_ebitda":("evebitda",),
 "sector_pe":("sectorpe",),"sector_pb":("sectorpb",),"sector_ev_ebitda":("sectorevebitda",),
 "historical_pe_median":("historicalpemedian",),"historical_pb_median":("historicalpbmedian",),"historical_ev_ebitda_median":("historicalevebitdamedian",),
 "gross_margin":("grossmargin",),"operating_margin":("operatingmargin",),"ebitda_margin":("ebitdamargin",),"net_margin":("netmargin",)}

_LABELS={
 "hasılat":"revenue","hasilat":"revenue","revenue":"revenue","satış gelirleri":"revenue",
 "satışlar":"revenue","mal ve hizmet satışlarından elde edilen gelirler":"revenue",
 "brüt kar":"gross_profit","brüt kâr":"gross_profit","gross profit":"gross_profit",
 "brüt kâr (zarar)":"gross_profit","brüt kar (zarar)":"gross_profit",
 "faaliyet karı":"operating_profit","faaliyet kârı":"operating_profit","operating profit":"operating_profit",
 "esas faaliyet kârı (zararı)":"operating_profit","esas faaliyet karı (zararı)":"operating_profit",
 "favök":"ebitda","favok":"ebitda","ebitda":"ebitda","dönem karı":"net_income","dönem kârı":"net_income",
 "net dönem karı":"net_income","net dönem kârı":"net_income","net income":"net_income",
 "net dönem kârı (zararı)":"net_income","net dönem karı (zararı)":"net_income",
 "dönem net kârı (zararı)":"net_income","dönem net karı (zararı)":"net_income",
 "net faiz geliri":"net_interest_income","net interest income":"net_interest_income",
 "krediler":"loans","krediler ve alacaklar":"loans","loans and receivables":"loans",
 "mevduat":"deposits","müşteri mevduatları":"deposits","customer deposits":"deposits",
 "işletme faaliyetlerinden nakit akışları":"operating_cash_flow","operating cash flow":"operating_cash_flow",
 "işletme faaliyetlerinden elde edilen nakit akışları":"operating_cash_flow",
 "faaliyetlerden elde edilen nakit akışları":"operating_cash_flow",
 "yatırım faaliyetlerinden nakit akışları":"investing_cash_flow","investing cash flow":"investing_cash_flow",
 "maddi duran varlık alımları":"capex","maddi duran varlık satın alımları":"capex","capital expenditures":"capex",
 "nakit ve nakit benzerleri":"cash_and_equivalents","cash and cash equivalents":"cash_and_equivalents",
 "kısa vadeli borçlanmalar":"short_term_financial_debt","short-term borrowings":"short_term_financial_debt",
 "uzun vadeli borçlanmalar":"long_term_financial_debt","long-term borrowings":"long_term_financial_debt",
 "toplam varlıklar":"total_assets","total assets":"total_assets","toplam yükümlülükler":"total_liabilities",
 "total liabilities":"total_liabilities","özkaynaklar":"equity","toplam özkaynaklar":"equity","özkaynak":"equity","equity":"equity",
 "ana ortaklığa ait özkaynaklar":"equity","shareholders equity":"equity"}

class _TableParser(HTMLParser):
    # A live KAP financial report contains many tiny taxonomy/layout tables.
    # Bound total cells and response bytes; a low table-count cap rejects valid
    # reports without providing meaningful resource protection.
    MAX_TABLES=5_000;MAX_CELLS=500_000
    def __init__(self):
        super().__init__();self.tables=[];self.text=[];self._contexts=[];self._cells=0
    def handle_starttag(self,tag,attrs):
        if tag=="table":
            if self._contexts:self._contexts[-1]["nested"]=True
            self._contexts.append({"table":[],"row":None,"cell":None,"rowspan":1,"colspan":1,"nested":False})
        elif tag=="tr" and self._contexts:self._contexts[-1]["row"]=[]
        elif tag in {"td","th"} and self._contexts and self._contexts[-1]["row"] is not None:
            values=dict(attrs);context=self._contexts[-1]
            context["rowspan"]=_span(values.get("rowspan"));context["colspan"]=_span(values.get("colspan"));context["cell"]=[]
    def handle_data(self,data):
        clean=" ".join(data.split())
        if clean:self.text.append(clean)
        if self._contexts and self._contexts[-1]["cell"] is not None and clean:self._contexts[-1]["cell"].append(clean)
    def handle_endtag(self,tag):
        if not self._contexts:return
        context=self._contexts[-1]
        if tag in {"td","th"} and context["cell"] is not None:
            context["row"].append((" ".join(context["cell"]),context["rowspan"],context["colspan"]));context["cell"]=None
        elif tag=="tr" and context["row"] is not None:
            if context["row"]:context["table"].append(context["row"])
            context["row"]=None
        elif tag=="table":
            context=self._contexts.pop()
            expanded=_expand_rows(context["table"]);self._cells+=sum(map(len,expanded))
            if len(self.tables)>=self.MAX_TABLES or self._cells>self.MAX_CELLS:raise ValueError(f"KAP HTML table complexity limit exceeded ({len(self.tables)} tables, {self._cells} cells)")
            self.tables.append(expanded)

def _span(value):
    try:number=int(value or 1)
    except ValueError:raise ValueError("invalid KAP table span")
    # Live KAP emits hidden taxonomy header cells with ``colspan="0"``.
    # Browsers treat that legacy value as spanning the remaining columns, but
    # for extraction the hidden placeholder must occupy one logical column.
    if number==0:return 1
    if number<1:raise ValueError("invalid KAP table span")
    # KAP uses very large spans in layout/taxonomy tables. Expansion is already
    # bounded to 64 logical columns, so clamp the declared span to that same cap.
    return min(number,64)
def _expand_rows(rows,max_columns=64):
    result=[];pending={}
    for raw in rows:
        slots={column:text for column,(_,text) in pending.items()}
        pending={column:(remaining-1,text) for column,(remaining,text) in pending.items() if remaining>1}
        column=0
        for text,rowspan,colspan in raw:
            placed=0
            while placed<colspan and column<max_columns:
                while column<max_columns and column in slots:column+=1
                if column>=max_columns:break
                slots[column]=text
                if rowspan>1:pending[column]=(rowspan-1,text)
                placed+=1;column+=1
        last=max(slots,default=-1)
        result.append([slots.get(index,"") for index in range(last+1)])
    return result

def _account_alias(label):
    normalized=_fold_label(label).replace("/"," ")
    direct=next((value for key,value in _LABELS.items() if _fold_label(key)==normalized),None)
    if direct:return direct
    compact=re.sub(r"[^a-z0-9]","",normalized)
    for name,aliases in _ALIASES.items():
        if any(re.sub(r"[^a-z0-9]","",str(alias).lower()) in compact for alias in aliases if "-" in str(alias)):
            return name
    # Current KAP pages commonly concatenate Turkish and English verbose labels.
    matched_labels=[value for key,value in _LABELS.items()
        if re.search(rf"(?:^|\s){re.escape(_fold_label(key))}(?:$|\s)",normalized)]
    matches=set(matched_labels)
    return next(iter(matches)) if len(matched_labels)>=2 and len(matches)==1 else None
def _fold_label(value):
    return " ".join("".join(char for char in unicodedata.normalize("NFKD",str(value).casefold())
        if not unicodedata.combining(char)).split()).strip(":")

def _first(data,*keys):
    if not isinstance(data,dict):return None
    folded={str(k).lower().replace("_",""):v for k,v in data.items()}
    for key in keys:
        if key in data:return data[key]
        if key.lower().replace("_","") in folded:return folded[key.lower().replace("_","")]
    return None
def _member_candidates(payload):
    """Normalize KAP's flat and current enveloped member-search responses."""
    if isinstance(payload,list):return [item for item in payload if isinstance(item,dict)]
    if not isinstance(payload,dict):return []
    for key in ("data","items","members","result","results","content"):
        nested=_first(payload,key)
        if isinstance(nested,list):return [item for item in nested if isinstance(item,dict)]
        if isinstance(nested,dict):return _member_candidates(nested)
    return [payload]

def _member_oid(payload,ticker):
    """Resolve the MKK OID from live KAP member response variants."""
    candidates=_member_candidates(payload)
    ticker=ticker.upper().removesuffix(".IS")
    exact=[candidate for candidate in candidates if ticker in _stock_codes(
        _first(candidate,"stockCode","stockCodes","ticker","symbol"))]
    if exact:candidates=exact
    matches=[]
    for candidate in candidates:
        oid=_first(candidate,"mkkMemberOid","memberOid","oid")
        if oid:matches.append(str(oid))
    unique=set(matches)
    return matches[0] if len(unique)==1 else None

class _KapHttpError(OSError):
    def __init__(self,status,retry_after=None):
        super().__init__(f"KAP HTTP {status}");self.status=status;self.retry_after=retry_after

def _retry_after(headers,*,now=lambda:datetime.now(timezone.utc)):
    if headers is None:return None
    value=headers.get("Retry-After") if hasattr(headers,"get") else None
    if value is None:return None
    try:delay=float(value)
    except (TypeError,ValueError):
        try:
            target=parsedate_to_datetime(str(value))
            if target.tzinfo is None:target=target.replace(tzinfo=timezone.utc)
            reference=now()
            if reference.tzinfo is None:reference=reference.replace(tzinfo=timezone.utc)
            delay=(target-reference.astimezone(timezone.utc)).total_seconds()
        except (TypeError,ValueError,OverflowError):return None
    return max(delay,0)

def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

def _calendar_year_windows(start,end):
    cursor=start
    while cursor<=end:
        window_end=min(end,cursor.replace(month=12,day=31))
        yield cursor,window_end
        cursor=window_end+timedelta(days=1)
def _stock_codes(value):
    values=value if isinstance(value,list) else re.split(r"[,;/\s]+",str(value or ""))
    return {str(item).strip().upper().removesuffix(".IS") for item in values
            if re.fullmatch(r"[A-Z0-9]{3,8}(?:\.IS)?",str(item).strip().upper())}
def _is_financial_statement_filing(filing):
    subject=_fold_label(_first(filing,"subject","disclosureSubject") or "")
    return not any(label in subject for label in ("sorumluluk beyan","representation letter",
        "faaliyet rapor","activity report","surdurulebilirlik rapor","sustainability report","tsrs"))
def _validated_shares(candidate):
    """Convert official paid capital only when KAP explicitly supplies the share rule."""
    if not isinstance(candidate,dict):return None
    paid=_number(_first(candidate,"paidCapital"));nominal=_number(_first(candidate,"nominalValuePerShare","nominalShareValue"))
    capital_currency=str(_first(candidate,"paidCapitalCurrency") or "").upper()
    nominal_currency=str(_first(candidate,"nominalValueCurrency") or "").upper()
    unit=str(_first(candidate,"shareUnit") or "").upper()
    if paid is None or nominal is None or paid<=0 or nominal<=0:return None
    if capital_currency not in {"TRY","TL"} or nominal_currency not in {"TRY","TL"} or unit not in {"SHARE","ADET"}:return None
    shares=paid/nominal
    return shares if shares.is_integer() else None
def _company_type(candidate):
    """Use KAP member metadata only; an unknown classification stays GENERAL."""
    if not isinstance(candidate,dict):return "GENERAL"
    metadata=" ".join(str(_first(candidate,key) or "") for key in
        ("companyType","memberTypeName","mainSector","sector","subSector","companyTitle","title","permalink"))
    folded=_fold_label(metadata)
    return "BANK" if any(token in folded for token in ("banka","bankasi","bankası","bank")) else "GENERAL"
def _date(value):
    if not value:return None
    if isinstance(value,datetime):return value
    text=str(value).replace("Z","+00:00")
    for fmt in (None,"%d.%m.%Y","%d.%m.%Y %H:%M:%S","%Y.%m.%d %H:%M:%S"):
        try:return datetime.fromisoformat(text) if fmt is None else datetime.strptime(text,fmt).replace(tzinfo=ZoneInfo("Europe/Istanbul"))
        except ValueError:pass
    return None
def _period_end(value):
    direct=_date(value)
    if direct:return direct
    text=str(value)
    match=re.search(r"[-–]\s*(\d{2}[./]\d{2}[./]\d{4})",text)
    if match:return _date(match.group(1))
    match=re.search(r"(\d{2}[./]\d{2}[./]\d{4})",text)
    if match:return _date(match.group(1))
    match=re.fullmatch(r"\s*(\d{4})/(3|6|9|12)\s*",text)
    if match:
        year,month=map(int,match.groups()); day={3:31,6:30,9:30,12:31}[month]
        return datetime(year,month,day,tzinfo=timezone.utc)
    return None
def _bool(value):
    if value is None:return None
    return value is True or str(value).lower() in {"true","1","consolidated","konsolide"}
def _unit(value):
    if value is None:return None
    if isinstance(value,(int,float)) and value in {1,1000,1_000_000}:return float(value)
    text=str(value).lower().replace(".","").replace(",","").strip()
    if "milyon" in text or text in {"1000000","mn"}:return 1_000_000.0
    if "bin" in text or text in {"1000","thousand"}:return 1_000.0
    if text in {"1","bir","tl","try"}:return 1.0
    if text in {"1000 tl","1000 try"}:return 1_000.0
    if text in {"1000000 tl","1000000 try"}:return 1_000_000.0
    return None
def _presentation(value):
    text=" ".join(str(value or "").upper().split())
    if not re.search(r"\b(TL|TRY)\b",text):return None,None
    return "TRY",_unit(text)
def _number(value):
    if value is None or isinstance(value,bool):return None
    if isinstance(value,(int,float)):return float(value)
    text=str(value).strip().replace(" ","")
    if text in {"","-","--","null","None"}:return None
    negative=text.startswith("(") and text.endswith(")"); text=text.strip("()")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+",text):text=text.replace(".","")
    elif "," in text and "." in text:text=text.replace(".","").replace(",",".")
    elif "," in text:text=text.replace(",",".")
    try:return -float(text) if negative else float(text)
    except ValueError:return None
def _facts(facts,unit):
    result={}
    iterable=facts.items() if isinstance(facts,dict) else ((str(_first(x,"code","name","taxonomy") or ""),_first(x,"value","amount")) for x in facts if isinstance(x,dict))
    for code,value in iterable:
        normalized=str(code).lower().replace("_","").replace("-","")
        for name,aliases in _ALIASES.items():
            if any(str(alias).lower().replace("_","").replace("-","")==normalized for alias in aliases):
                number=_number(value); result.setdefault(name,number*unit if number is not None else None)
    return result
def _metric(value,record): return Metric(value=value,available=value is not None,source=f"KAP:{_first(record,'filingId','disclosureIndex','id') or 'financial'}")
def _metric_value(value,source): return Metric(value=float(value),available=True,source=source)
def _sum_metrics(a,b,source): return _metric_value(a.value+b.value,source) if a.value is not None and b.value is not None else Metric()
def _subtract(a,b,source): return _metric_value(a.value-b.value,source) if a.value is not None and b.value is not None else Metric()
def _ratio(a,b,source): return _metric_value(a.value/b.value,source) if a.value is not None and b.value not in {None,0} else Metric()
def _ttm(periods,name):
    values=[getattr(p,name).value for p in periods[:4]]
    return sum(values) if len(values)==4 and all(v is not None for v in values) else None
def _normalize_revisions(periods):
    """Select one coherent statement per period; never synthesize across filings."""
    grouped={}
    for period in periods:grouped.setdefault(period.period_end.date(),[]).append(period)
    merged=[]
    for revisions in grouped.values():
        if any(period.consolidated is True for period in revisions):
            revisions=[period for period in revisions if period.consolidated is True]
        # A full-period statement is more coherent than a later comparative
        # instant column. Within the same basis, publication time resolves revisions.
        revisions.sort(key=lambda p:((p.period_end-p.period_start).days>=300 if p.period_start else False,
            sum(getattr(p,name).value is not None for name in _ALIASES),p.published_at or p.period_end),reverse=True)
        period=revisions[0].model_copy(deep=True)
        # Legacy hierarchy parsing could map the parent Assets label to Equity.
        # The accounting identity recovers equity only when liabilities are known.
        if (period.total_assets.value is not None and period.equity.value==period.total_assets.value):
            if period.total_liabilities.value is not None:
                period.equity=_metric_value(period.total_assets.value-period.total_liabilities.value,
                    "derived:assets_minus_liabilities")
            else:
                # A legacy hierarchy collision copied the parent Assets row into
                # Equity. Without liabilities the true equity cannot be recovered.
                period.equity=Metric()
        for target,numerator in (("gross_margin","gross_profit"),("operating_margin","operating_profit"),
                                  ("ebitda_margin","ebitda"),("net_margin","net_income")):
            if getattr(period,target).value is None:
                setattr(period,target,_ratio(getattr(period,numerator),period.revenue,f"derived:{target}"))
        merged.append(period)
    return _quarterize(merged)
def _quarterize(periods):
    """Turn KAP year-to-date flow statements into discrete quarters when identifiable."""
    flows=("revenue","gross_profit","operating_profit","ebitda","net_income","net_interest_income","operating_cash_flow","investing_cash_flow","capex","free_cash_flow")
    by_year={}
    for period in periods:by_year.setdefault(period.period_end.year,[]).append(period)
    for group in by_year.values():
        group.sort(key=lambda p:p.period_end)
        previous_values=None
        for period in group:
            current_values={name:getattr(period,name).value for name in flows}
            cumulative=(period.period_start is not None and period.period_start.month==1 and period.period_end.month>3)
            if cumulative and previous_values is not None:
                for name in flows:
                    current=getattr(period,name); prior_value=previous_values[name]
                    if current.value is not None and prior_value is not None:
                        setattr(period,name,_metric_value(current.value-prior_value,f"derived:quarter_from_ytd:{current.source}"))
                for target,numerator in (("gross_margin","gross_profit"),("operating_margin","operating_profit"),
                                          ("ebitda_margin","ebitda"),("net_margin","net_income")):
                    setattr(period,target,_ratio(getattr(period,numerator),period.revenue,f"derived:{target}"))
            previous_values=current_values
    return sorted(periods,key=lambda p:p.period_end,reverse=True)
