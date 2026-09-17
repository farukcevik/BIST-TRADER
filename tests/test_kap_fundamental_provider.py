from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
from email.utils import format_datetime
import urllib.error
import threading
import pytest

from bistbot.fundamental import (FinancialPeriod,FundamentalEngine,FundamentalProviderStatus,
    KapFundamentalProvider,Metric)
from bistbot.fundamental.provider import _account_alias,_company_type,_validated_shares
from bistbot.storage.database import Database

NOW=datetime(2026,2,16,tzinfo=timezone.utc)
def m(value):return Metric(value=value,available=True,source="fixture")

def test_official_capital_conversion_requires_explicit_share_rule():
    assert _validated_shares({"paidCapital":1000,"nominalValuePerShare":1,"paidCapitalCurrency":"TRY",
        "nominalValueCurrency":"TRY","shareUnit":"ADET"})==1000
    assert _validated_shares({"paidCapital":1000,"nominalValuePerShare":1}) is None

def test_kap_metadata_and_bank_labels_select_bank_profile_inputs():
    assert _company_type({"sector":"Bankacılık","title":"Örnek Bankası"})=="BANK"
    assert _company_type({"sector":"Perakende","title":"Örnek AŞ"})=="GENERAL"
    assert [_account_alias(label) for label in ("Net Faiz Geliri","Krediler ve Alacaklar","Müşteri Mevduatları")]==[
        "net_interest_income","loans","deposits"]

class Response:
    status=200
    def __init__(self,payload,*,status=200,headers=None):self.payload=json.dumps(payload).encode();self.status=status;self.headers=headers or {}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self):return self.payload
class RawResponse(Response):
    def __init__(self,payload):self.payload=payload.encode()

class FixtureOpener:
    def __init__(self,financials):self.financials=financials;self.calls=[];self.filing_id=9008;self.published="15.02.2026 10:00:00"
    def __call__(self,request,timeout):
        self.calls.append((request.full_url,timeout))
        if "/member/filter/" in request.full_url:return Response([{"companyCode":"5997","mkkMemberOid":"official-oid","title":"DCT TRADING","permaLink":"5997-dct-trading"}])
        if "/api/disclosure/" in request.full_url:
            return Response([{"disclosureClass":"FR","stockCodes":"AAA BBB DCTTR","disclosureIndex":self.filing_id,"publishDate":self.published}])
        return RawResponse(self.financials)

def fixture():
    return (Path(__file__).parent/"fixtures"/"kap_company_financial_information.html").read_text()
def json_fixture(name):return json.loads((Path(__file__).parent/"fixtures"/name).read_text())

def test_real_member_list_shape_and_multiple_disclosures_are_processed_once(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();calls=[]
    def opener(request,timeout):
        calls.append(request.full_url)
        if "/member/filter/" in request.full_url:return Response(json_fixture("kap_member_filter_dcttr.json"))
        if "/api/disclosure/" in request.full_url:return Response(json_fixture("kap_financial_disclosures.json"))
        return RawResponse(fixture())
    provider=KapFundamentalProvider(str(path),opener=opener,sleeper=lambda _:None,min_request_interval=0)
    analysis_time=NOW+timedelta(hours=8)
    first=provider.get_snapshot("DCTTR.IS",analysis_time);provider.get_snapshot("DCTTR.IS",analysis_time+timedelta(hours=7))
    assert first.periods and len([url for url in calls if "/Bildirim/" in url])==2
    with Database(str(path)) as db:
        assert db.query("SELECT COUNT(*) n FROM kap_processed_disclosures WHERE symbol='DCTTR'")[0]["n"]==2

def test_live_member_envelope_selects_exact_stock_code(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close()
    payload={"data":{"items":[
        {"stockCode":"DCTTR","mkkMemberOid":"dcttr-mkk","kapMemberOid":"not-used"},
        {"stockCode":"DCT","mkkMemberOid":"other-mkk"}]}}
    provider=KapFundamentalProvider(str(path),opener=lambda request,timeout:Response(payload),
        sleeper=lambda _:None,min_request_interval=0)
    assert provider._member_identity("DCTTR",NOW)=="dcttr-mkk"

def test_ambiguous_member_search_uses_active_company_catalog(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();calls=[]
    search=[{"mkkMemberOid":"wrong","title":"MEDİTERA"},{"mkkMemberOid":"also-wrong","title":"TERA FİNANS"}]
    catalog=[{"stockCode":"TERA","mkkMemberOid":"tera-oid","companyCode":"968"}]
    def opener(request,timeout):
        calls.append(request.full_url)
        return Response(catalog if "/company/items/" in request.full_url else search)
    provider=KapFundamentalProvider(str(path),opener=opener,sleeper=lambda _:None,min_request_interval=0)
    assert provider._member_identity("TERA",NOW)=="tera-oid"
    assert calls==[provider.MEMBER_URL.format(ticker="TERA"),provider.MEMBER_CATALOG_URL]

def test_kap_normalizes_caches_and_does_not_refetch_unchanged_history(tmp_path):
    path=tmp_path/"kap.sqlite"; Database(str(path)).close(); opener=FixtureOpener(fixture())
    provider=KapFundamentalProvider(str(path),opener=opener,sleeper=lambda _:None,
        min_request_interval=0,price_resolver=lambda _:(10,NOW))
    first=provider.get_snapshot("AAA.IS",NOW); second=provider.get_snapshot("AAA.IS",NOW)
    assert len([url for url,_ in opener.calls if "/Bildirim/" in url])==1 and len(first.periods)==8 and second.periods==first.periods
    latest=first.periods[0]
    assert latest.revenue.value==10_000_000 and latest.total_debt.value==30_000_000
    assert latest.net_debt.value==-10_000_000 and latest.free_cash_flow.value==0
    assert latest.market_cap.value is None and not latest.pe.available
    assert latest.total_liabilities.available and latest.gross_margin.value==.3
    assert first.audit_metadata["official_source"]=="https://www.kap.org.tr"
    assert first.provider_status is FundamentalProviderStatus.AVAILABLE
    derived=FundamentalEngine().evaluate(first).derived_metrics
    assert derived["yoy_revenue_growth"].available and derived["revenue_cagr"].available

def test_kap_failure_uses_cache_and_missing_values_remain_none(tmp_path):
    path=tmp_path/"kap.sqlite"; Database(str(path)).close(); good=FixtureOpener(fixture());clock=[NOW]
    KapFundamentalProvider(str(path),opener=good,sleeper=lambda _:None,min_request_interval=0,now=lambda:clock[0]).get_snapshot("AAA.IS",NOW)
    def fail(*args,**kwargs):raise TimeoutError("offline")
    later=datetime(2026,2,17,tzinfo=timezone.utc)
    clock[0]=later
    result=KapFundamentalProvider(str(path),opener=fail,sleeper=lambda _:None,min_request_interval=0,now=lambda:clock[0]).get_snapshot("AAA.IS",later)
    assert result.periods and result.audit_metadata["refresh_error"].startswith("TimeoutError")
    assert result.periods[1].outstanding_shares.value is None and not result.periods[1].outstanding_shares.available
    assert result.audit_metadata["source_url"].endswith("/Bildirim/9008") and result.audit_metadata["fetched_at"]
    clock[0]=NOW+timedelta(days=46)
    stale=KapFundamentalProvider(str(path),opener=fail,sleeper=lambda _:None,min_request_interval=0,now=lambda:clock[0],
        max_cache_age=timedelta(days=45)).get_snapshot("AAA.IS",NOW+timedelta(days=46))
    assert stale.provider_status is FundamentalProviderStatus.UNAVAILABLE and stale.periods

def test_kap_empty_or_invalid_payload_is_unavailable(tmp_path):
    path=tmp_path/"kap.sqlite"; Database(str(path)).close(); opener=FixtureOpener("<html>invalid</html>")
    result=KapFundamentalProvider(str(path),opener=opener,sleeper=lambda _:None,min_request_interval=0).get_snapshot("AAA.IS",NOW)
    assert result.provider_status is FundamentalProviderStatus.UNAVAILABLE and result.periods==[]

def test_retry_timeout_rate_limit_and_short_failure_backoff(tmp_path):
    path=tmp_path/"kap.sqlite"; Database(str(path)).close(); calls=[]; sleeps=[];clock=[NOW]
    def fail(request,timeout):calls.append((request.full_url,timeout));raise TimeoutError("offline")
    provider=KapFundamentalProvider(str(path),opener=fail,sleeper=sleeps.append,max_attempts=3,
        timeout_seconds=7,min_request_interval=.1,failure_backoff=timedelta(minutes=10),now=lambda:clock[0])
    provider.get_snapshot("AAA.IS",NOW); provider.get_snapshot("AAA.IS",NOW+timedelta(minutes=5))
    assert len(calls)==3 and all(timeout==7 for _,timeout in calls)
    assert any(.49<=delay<=.5 for delay in sleeps) and any(.99<=delay<=1.0 for delay in sleeps)
    clock[0]=NOW+timedelta(minutes=11);provider.get_snapshot("AAA.IS",NOW)
    assert len(calls)==6

def test_http_429_honors_retry_after(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();calls=[];sleeps=[]
    def opener(request,timeout):
        calls.append(request.full_url)
        if len(calls)==1:return Response({},status=429,headers={"Retry-After":"3"})
        return Response([{"mkkMemberOid":"oid"}])
    provider=KapFundamentalProvider(str(path),opener=opener,sleeper=sleeps.append,
        max_attempts=2,min_request_interval=0)
    assert provider._member_identity("AAA",NOW)=="oid"
    assert calls==[provider.MEMBER_URL.format(ticker="AAA")]*2
    assert len(sleeps)==1 and 2.99<=sleeps[0]<=3.0

def test_final_429_publishes_retry_after_to_other_instances(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();calls=[];sleeps=[];ticks=[0.0]
    def sleep(delay):sleeps.append(delay);ticks[0]+=delay
    def opener(request,timeout):
        calls.append((request.full_url,ticks[0]))
        if len(calls)==1:return Response({},status=429,headers={"Retry-After":"120"})
        return Response([{"mkkMemberOid":"oid"}])
    kwargs={"opener":opener,"sleeper":sleep,"monotonic":lambda:ticks[0],
        "min_request_interval":0,"max_attempts":1}
    first=KapFundamentalProvider(str(path),**kwargs);second=KapFundamentalProvider(str(path),**kwargs)
    with pytest.raises(OSError,match="KAP HTTP 429"):
        first._member_identity("AAA",NOW)
    assert second._member_identity("BBB",NOW)=="oid"
    assert calls[1][1]-calls[0][1]>=120 and sleeps==[120.0]

def test_http_error_429_accepts_http_date_retry_after(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();calls=[];sleeps=[]
    retry_at=datetime.now(timezone.utc)+timedelta(seconds=20)
    def opener(request,timeout):
        calls.append(request.full_url)
        if len(calls)==1:
            raise urllib.error.HTTPError(request.full_url,429,"rate limited",
                {"Retry-After":format_datetime(retry_at,usegmt=True)},None)
        return Response([{"mkkMemberOid":"oid"}])
    provider=KapFundamentalProvider(str(path),opener=opener,sleeper=sleeps.append,
        max_attempts=2,min_request_interval=0)
    assert provider._member_identity("AAA",NOW)=="oid"
    assert len(calls)==2 and len(sleeps)==1 and 15<=sleeps[0]<=20

def test_rate_limiter_is_shared_across_provider_instances(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();calls=[];sleeps=[];ticks=[0.0]
    def sleep(delay):sleeps.append(delay);ticks[0]+=delay
    def opener(request,timeout):calls.append((request.full_url,ticks[0]));return Response([{"mkkMemberOid":"oid"}])
    kwargs={"opener":opener,"sleeper":sleep,"monotonic":lambda:ticks[0],"min_request_interval":.5}
    first=KapFundamentalProvider(str(path),**kwargs);second=KapFundamentalProvider(str(path),**kwargs)
    first._member_identity("AAA",NOW);second._member_identity("BBB",NOW)
    assert calls[1][1]-calls[0][1]>=.5 and sleeps==[.5]

def test_default_kap_jitter_is_between_one_point_five_and_two_point_five_seconds(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();calls=[];sleeps=[];ticks=[0.0]
    def sleep(delay):sleeps.append(delay);ticks[0]+=delay
    def opener(request,timeout):calls.append(ticks[0]);return Response([{"mkkMemberOid":"oid"}])
    provider=KapFundamentalProvider(str(path),opener=opener,sleeper=sleep,
        monotonic=lambda:ticks[0],jitter=lambda low,high:2.0)
    provider._member_identity("AAA",NOW);provider._member_identity("BBB",NOW)
    assert calls==[0.0,2.0] and sleeps==[2.0]

def test_kap_requests_are_serialized_across_provider_instances(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();entered=[];first_entered=threading.Event();release=threading.Event()
    def opener(request,timeout):
        entered.append(request.full_url)
        if len(entered)==1:first_entered.set();release.wait(timeout=2)
        return Response([{"mkkMemberOid":"oid"}])
    first=KapFundamentalProvider(str(path),opener=opener,min_request_interval=0)
    second=KapFundamentalProvider(str(path),opener=opener,min_request_interval=0)
    threads=[threading.Thread(target=provider._member_identity,args=(ticker,NOW))
        for provider,ticker in ((first,"AAA"),(second,"BBB"))]
    for thread in threads:thread.start()
    assert first_entered.wait(timeout=1) and len(entered)==1
    release.set()
    for thread in threads:thread.join(timeout=2)
    assert len(entered)==2 and all(not thread.is_alive() for thread in threads)

def test_concurrent_429_registers_retry_after_before_next_request(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();calls=[];sleeps=[];ticks=[0.0]
    first_entered=threading.Event();release=threading.Event();errors=[]
    def sleep(delay):sleeps.append(delay);ticks[0]+=delay
    def opener(request,timeout):
        calls.append(ticks[0])
        if len(calls)==1:
            first_entered.set();release.wait(timeout=2);return Response({},status=429,headers={"Retry-After":"3"})
        return Response([{"mkkMemberOid":"oid"}])
    kwargs={"opener":opener,"sleeper":sleep,"monotonic":lambda:ticks[0],"min_request_interval":0,"max_attempts":1}
    first=KapFundamentalProvider(str(path),**kwargs);second=KapFundamentalProvider(str(path),**kwargs)
    def call(provider,ticker):
        try:provider._member_identity(ticker,NOW)
        except OSError as exc:errors.append(exc)
    a=threading.Thread(target=call,args=(first,"AAA"));b=threading.Thread(target=call,args=(second,"BBB"))
    a.start();assert first_entered.wait(timeout=1);b.start();release.set();a.join(timeout=2);b.join(timeout=2)
    assert len(errors)==1 and calls==[0.0,3.0] and sleeps==[3.0]

def test_legacy_member_without_company_type_is_refreshed_and_classified(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();calls=[]
    with Database(str(path)) as db:
        db.execute("INSERT INTO kap_member_cache(symbol,mkk_member_oid,resolved_at,company_type) VALUES(?,?,?,NULL)",
            ("BANK","legacy-oid",NOW.isoformat()))
    def opener(request,timeout):
        calls.append(request.full_url)
        return Response([{"mkkMemberOid":"legacy-oid","sector":"Bankacılık","title":"Örnek Bankası"}])
    provider=KapFundamentalProvider(str(path),opener=opener,sleeper=lambda _:None,min_request_interval=0)
    assert provider._member_identity("BANK",NOW)=="legacy-oid"
    assert provider._member_company_type("BANK")=="BANK"
    provider._member_identity("BANK",NOW+timedelta(days=1))
    assert len(calls)==1

def test_refresh_backoff_uses_wall_clock_not_analysis_cutoff(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();calls=[];wall=[NOW]
    def fail(request,timeout):calls.append(request.full_url);raise TimeoutError("offline")
    provider=KapFundamentalProvider(str(path),opener=fail,sleeper=lambda _:None,
        min_request_interval=0,max_attempts=1,now=lambda:wall[0])
    provider.get_snapshot("AAA.IS",NOW)
    provider.get_snapshot("AAA.IS",NOW+timedelta(days=100))
    assert len(calls)==1
    wall[0]+=timedelta(minutes=11)
    provider.get_snapshot("AAA.IS",NOW)
    assert len(calls)==2
    with Database(str(path)) as db:
        checked=db.query("SELECT last_checked_at FROM kap_fundamental_refresh WHERE symbol='AAA'")[0]["last_checked_at"]
    assert checked==wall[0].isoformat()

def test_discovery_excludes_same_day_filing_after_analysis_cutoff(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();details=[]
    cutoff=datetime(2026,2,16,6,0,tzinfo=timezone.utc)
    def opener(request,timeout):
        if "/member/filter/" in request.full_url:return Response([{"mkkMemberOid":"oid"}])
        if "/api/disclosure/" in request.full_url:
            return Response([{"disclosureClass":"FR","stockCodes":"AAA","disclosureIndex":99,
                "publishDate":"16.02.2026 10:00:00","subject":"Finansal Rapor"}])
        details.append(request.full_url);return RawResponse(fixture())
    result=KapFundamentalProvider(str(path),opener=opener,sleeper=lambda _:None,
        min_request_interval=0).get_snapshot("AAA.IS",cutoff)
    assert result.periods==[] and result.provider_status is FundamentalProviderStatus.UNAVAILABLE
    assert details==[]

def test_future_cached_filing_is_excluded_from_historical_snapshot(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();opener=FixtureOpener(fixture())
    opener.published="16.02.2026 10:00:00"
    provider=KapFundamentalProvider(str(path),opener=opener,sleeper=lambda _:None,min_request_interval=0)
    future_cutoff=datetime(2026,2,16,11,0,tzinfo=timezone.utc)
    assert provider.get_snapshot("AAA.IS",future_cutoff).periods
    historical=provider.get_snapshot("AAA.IS",datetime(2026,2,16,6,0,tzinfo=timezone.utc))
    assert historical.periods==[] and historical.provider_status is FundamentalProviderStatus.UNAVAILABLE

def test_html_currency_unit_and_partial_semantics(tmp_path):
    path=tmp_path/"kap.sqlite"; Database(str(path)).close()
    provider=KapFundamentalProvider(str(path),opener=FixtureOpener(fixture()),sleeper=lambda _:None,min_request_interval=0)
    assert provider._parse_html(fixture().replace("TRY","USD",1))==[]
    assert provider._parse_html(fixture().replace("Bin TL","Lots").replace("<td>TRY</td>","<td>USD</td>"))==[]
    sparse=fixture().replace("<tr><td>Hasılat", "<tr><td>Unsupported Revenue")
    result=KapFundamentalProvider(str(path),opener=FixtureOpener(sparse),sleeper=lambda _:None,min_request_interval=0).get_snapshot("BBB.IS",NOW)
    assert result.provider_status is FundamentalProviderStatus.PARTIAL
    prefixed=provider._parse_html(fixture().replace("2025/12","Cari Dönem 31.12.2025",1))
    assert prefixed[0].period_end.date().isoformat()=="2025-12-31" and prefixed[0].period_start is None
    ranged=provider._parse_html(fixture().replace("2025/12","01.01.2025 - 31.12.2025",1))
    assert ranged[0].period_start.date().isoformat()=="2025-01-01"

def test_mid_batch_parse_failure_is_atomic_and_retryable(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close()
    disclosures=json_fixture("kap_financial_disclosures.json")
    def opener(request,timeout):
        if "/member/filter/" in request.full_url:return Response(json_fixture("kap_member_filter_dcttr.json"))
        if "/api/disclosure/" in request.full_url:return Response(disclosures)
        return RawResponse(fixture() if request.full_url.endswith("/9009") else "<html>invalid</html>")
    result=KapFundamentalProvider(str(path),opener=opener,sleeper=lambda _:None,min_request_interval=0).get_snapshot("DCTTR.IS",NOW)
    with Database(str(path)) as db:
        assert db.query("SELECT COUNT(*) n FROM kap_processed_disclosures")[0]["n"]==0
        assert db.query("SELECT COUNT(*) n FROM kap_raw_filing_periods")[0]["n"]==0
    assert result.provider_status is FundamentalProviderStatus.UNAVAILABLE

def test_multi_table_facts_merge_by_period_and_scope(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();provider=KapFundamentalProvider(str(path))
    html="""<div>Sunum Para Birimi: 1000 TL</div><div>Konsolide</div>
    <table><tr><th>Kalem</th><th>Dipnot</th><th>Cari Dönem 31.12.2025</th></tr>
      <tr><td>Toplam Özkaynaklar</td><td>27</td><td>20.000</td></tr></table>
    <table><tr><th>Kalem</th><th>Dipnot</th><th>01.01.2025 - 31.12.2025</th></tr>
      <tr><td rowspan="1">Hasılat</td><td>5</td><td>10.000</td></tr></table>"""
    periods=provider._parse_html(html)
    assert len(periods)==1 and periods[0].period_start.date().isoformat()=="2025-01-01"
    assert periods[0].revenue.value==10_000_000 and periods[0].equity.value==20_000_000

def test_current_kap_bilingual_xbrl_rows_and_multi_column_labels_parse(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();provider=KapFundamentalProvider(str(path))
    html="""<div>Sunum Para Birimi: TL</div><div>Konsolide</div><table>
      <tr><th>Taksonomi</th><th>Finansal Kalem</th><th>Dipnot</th><th>Cari Dönem 01.01.2026 - 31.03.2026</th></tr>
      <tr><td>ifrs-full_Revenue</td><td>Hasılat Revenue</td><td>30</td><td>77.661.544</td></tr>
      <tr><td>ifrs-full_Assets</td><td>Toplam Varlıklar Total assets</td><td></td><td>250.000.000</td></tr>
      <tr><td>ifrs-full_Equity</td><td>Toplam Özkaynaklar Equity</td><td></td><td>100.000.000</td></tr>
      <tr><td>ifrs-full_ProfitLoss</td><td>Net Dönem Kârı (Zararı) Net income</td><td></td><td>8.000.000</td></tr>
    </table>"""
    periods=provider._parse_html(html)
    assert len(periods)==1 and periods[0].revenue.value==77_661_544
    assert periods[0].total_assets.value==250_000_000 and periods[0].net_income.value==8_000_000

def test_specific_hierarchy_label_wins_and_cached_revisions_are_not_synthesized(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();provider=KapFundamentalProvider(str(path))
    html="""<div>Sunum Para Birimi: TL</div><table>
      <tr><th>Parent</th><th>Leaf</th><th>31.12.2025</th></tr>
      <tr><td>Toplam Varlıklar</td><td>Toplam Özkaynaklar</td><td>400</td></tr></table>"""
    parsed=provider._parse_html(html,quarterize=False)
    assert parsed[0].equity.value==400 and parsed[0].total_assets.value is None
    annual=FinancialPeriod(period_end=datetime(2025,12,31,tzinfo=timezone.utc),
        period_start=datetime(2025,1,1,tzinfo=timezone.utc),published_at=NOW-timedelta(days=30),
        revenue=m(1000),net_income=m(100),operating_cash_flow=m(120),total_assets=m(800),
        total_liabilities=m(300),equity=m(800))
    comparative=FinancialPeriod(period_end=annual.period_end,published_at=NOW,
        total_assets=m(900),total_liabilities=m(350),equity=m(900))
    with Database(str(path)) as db:
        for index,period in enumerate((annual,comparative)):
            db.execute("INSERT INTO kap_raw_filing_periods VALUES(?,?,?,?,?,?)",
                ("AAA",str(index),period.period_end.isoformat(),1,period.published_at.isoformat(),period.model_dump_json()))
    loaded=provider._load("AAA")
    assert loaded[0].revenue.value==1000 and loaded[0].operating_cash_flow.value==120
    assert loaded[0].total_assets.value==800 and loaded[0].equity.value==500

def test_cached_assets_misparsed_as_equity_stays_unknown_without_liabilities(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();provider=KapFundamentalProvider(str(path))
    period=FinancialPeriod(period_end=datetime(2025,12,31,tzinfo=timezone.utc),
        period_start=datetime(2025,1,1,tzinfo=timezone.utc),published_at=NOW,
        net_income=m(20),equity=m(900),total_assets=m(900))
    with Database(str(path)) as db:
        db.execute("INSERT INTO kap_raw_filing_periods VALUES(?,?,?,?,?,?)",
            ("BANK","filing",period.period_end.isoformat(),0,NOW.isoformat(),period.model_dump_json()))
    loaded=provider._load("BANK")
    assert loaded[0].total_assets.value==900
    assert loaded[0].equity.value is None

def test_equity_equal_to_assets_is_valid_when_zero_liabilities_are_available(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();provider=KapFundamentalProvider(str(path))
    period=FinancialPeriod(period_end=datetime(2025,12,31,tzinfo=timezone.utc),
        period_start=datetime(2025,1,1,tzinfo=timezone.utc),published_at=NOW,
        equity=m(900),total_assets=m(900),total_liabilities=m(0))
    with Database(str(path)) as db:
        db.execute("INSERT INTO kap_raw_filing_periods VALUES(?,?,?,?,?,?)",
            ("AAA","filing",period.period_end.isoformat(),1,NOW.isoformat(),period.model_dump_json()))
    loaded=provider._load("AAA")
    assert loaded[0].equity.value==900
    assert loaded[0].equity.source=="derived:assets_minus_liabilities"

def test_cached_permalink_classifies_bank_without_ticker_heuristic(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close()
    with Database(str(path)) as db:
        db.execute("INSERT INTO kap_member_cache(symbol,mkk_member_oid,permalink,resolved_at) VALUES(?,?,?,?)",
            ("XYZ","oid","ornek-katilim-bankasi-a-s",NOW.isoformat()))
    provider=KapFundamentalProvider(str(path))
    assert provider._member_company_profile("XYZ")==("BANK","INFERRED","KAP_PERMALINK")
    assert provider._member_company_profile("MISSING")==("GENERAL","PROVISIONAL","DEFAULT")

def test_discovery_splits_at_result_limit_and_keeps_fr_filter_empty(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();bodies=[]
    def opener(request,timeout):
        body=json.loads(request.data);bodies.append(body)
        start=datetime.fromisoformat(body["fromDate"]).date();end=datetime.fromisoformat(body["toDate"]).date()
        return Response([{"disclosureIndex":index} for index in range(250)] if (end-start).days>10 else [])
    provider=KapFundamentalProvider(str(path),opener=opener,sleeper=lambda _:None,min_request_interval=0)
    assert provider._discover("oid",NOW.date()-timedelta(days=40),NOW.date())==[]
    assert len(bodies)>1 and all(body["disclosureClass"]=="" for body in bodies)
    assert all("inactiveMkkMemberOidList" in body and body["memberType"]=="IGS" for body in bodies)

def test_bootstrap_discovery_stops_after_newest_year_with_filings(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();windows=[]
    def opener(request,timeout):
        if "/member/filter/" in request.full_url:return Response(json_fixture("kap_member_filter_dcttr.json"))
        if "/Bildirim/" in request.full_url:return RawResponse(fixture())
        body=json.loads(request.data);windows.append(body)
        return Response([{"disclosureClass":"FR","stockCodes":"DCTTR","disclosureIndex":1,
            "publishDate":"15.02.2026 10:00:00","subject":"Finansal Rapor"}])
    KapFundamentalProvider(str(path),opener=opener,sleeper=lambda _:None,min_request_interval=0).get_snapshot("DCTTR.IS",NOW)
    assert len(windows)==1
    assert windows[0]["fromDate"][:4]==windows[0]["toDate"][:4]=="2026"
    assert all(item["disclosureClass"]=="" for item in windows)

def test_bootstrap_continues_past_year_with_only_excluded_companion_filing(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();windows=[]
    def opener(request,timeout):
        if "/member/filter/" in request.full_url:return Response(json_fixture("kap_member_filter_dcttr.json"))
        if "/Bildirim/" in request.full_url:return RawResponse(fixture())
        body=json.loads(request.data);windows.append(body)
        if body["fromDate"].startswith("2026"):
            return Response([{"disclosureClass":"FR","stockCodes":"DCTTR","disclosureIndex":2,
                "publishDate":"15.02.2026 10:00:00","subject":"TSRS Uyumlu Sürdürülebilirlik Raporu"}])
        return Response([{"disclosureClass":"FR","stockCodes":"DCTTR","disclosureIndex":1,
            "publishDate":"31.12.2025 10:00:00","subject":"Finansal Rapor"}])
    result=KapFundamentalProvider(str(path),opener=opener,sleeper=lambda _:None,
        min_request_interval=0).get_snapshot("DCTTR.IS",NOW)
    assert [item["fromDate"][:4] for item in windows]==["2026","2025"]
    assert result.periods and result.audit_metadata["refresh_error"] is None

def test_stock_code_shapes_are_supported():
    from bistbot.fundamental.provider import _is_financial_statement_filing,_stock_codes
    assert _stock_codes(["DCTTR","THYAO.IS"])=={"DCTTR","THYAO"}
    assert _stock_codes("DCTTR; THYAO/EREGL,AKBNK") == {"DCTTR","THYAO","EREGL","AKBNK"}
    assert not _is_financial_statement_filing({"subject":"Sorumluluk Beyanı (Konsolide)"})
    assert not _is_financial_statement_filing({"subject":"Faaliyet Raporu (Konsolide)"})
    assert not _is_financial_statement_filing({"subject":"TSRS Uyumlu Sürdürülebilirlik Raporu"})
    assert _is_financial_statement_filing({"subject":"Finansal Rapor"})

def test_implausibly_old_report_downgrades_even_when_discovery_succeeds(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();opener=FixtureOpener(fixture())
    provider=KapFundamentalProvider(str(path),opener=opener,sleeper=lambda _:None,min_request_interval=0)
    provider.get_snapshot("AAA.IS",NOW)
    stale=provider.get_snapshot("AAA.IS",datetime(2027,9,1,tzinfo=timezone.utc))
    assert stale.audit_metadata["refresh_error"] is None and stale.provider_status is FundamentalProviderStatus.UNAVAILABLE

def test_established_cache_treats_no_new_filing_as_success(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();opener=FixtureOpener(fixture());clock=[NOW]
    provider=KapFundamentalProvider(str(path),opener=opener,sleeper=lambda _:None,min_request_interval=0,now=lambda:clock[0])
    first=provider.get_snapshot("AAA.IS",NOW)
    assert first.periods and first.audit_metadata["refresh_error"] is None

    original=opener.__call__
    def no_new_filings(request,timeout):
        if "/api/disclosure/" in request.full_url:
            opener.calls.append((request.full_url,timeout))
            return Response([])
        return original(request,timeout)
    opener.__call__=no_new_filings
    # Special-method lookup happens on the class, so use a narrow callable wrapper.
    provider.opener=lambda request,timeout:no_new_filings(request,timeout)
    later=NOW+timedelta(hours=7)
    clock[0]=later;cached=provider.get_snapshot("AAA.IS",NOW)
    assert cached.periods==first.periods
    assert cached.audit_metadata["refresh_error"] is None
    assert cached.audit_metadata["discovery_checked_at"]==later.isoformat()
    with Database(str(path)) as db:
        row=db.query("SELECT last_checked_at,last_success_at,error FROM kap_fundamental_refresh WHERE symbol='AAA'")[0]
    assert row["last_checked_at"]==later.isoformat()
    assert row["last_success_at"]==later.isoformat()
    assert row["error"] is None

def test_member_identity_cache_expires_after_thirty_days(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close();opener=FixtureOpener(fixture())
    provider=KapFundamentalProvider(str(path),opener=opener,sleeper=lambda _:None,min_request_interval=0)
    assert provider._member_identity("DCTTR",NOW)=="official-oid"
    provider._member_identity("DCTTR",NOW+timedelta(days=29))
    assert len([url for url,_ in opener.calls if "/member/filter/" in url])==1
    provider._member_identity("DCTTR",NOW+timedelta(days=31))
    assert len([url for url,_ in opener.calls if "/member/filter/" in url])==2

def test_table_parser_expands_spans_and_rejects_excessive_span():
    from bistbot.fundamental.provider import _TableParser
    parser=_TableParser();parser.feed("<table><tr><th colspan='2'>Header</th></tr><tr><td rowspan='2'>A</td><td>1</td></tr><tr><td>2</td></tr></table>")
    assert parser.tables[0]==[["Header","Header"],["A","1"],["A","2"]]
    parser=_TableParser();parser.feed("<table><tr><td colspan='0'>KAP hidden header</td><td>Value</td></tr></table>")
    assert parser.tables[0]==[["KAP hidden header","Value"]]
    parser=_TableParser();parser.feed("<table><tr><td>Layout<table><tr><td>Financial</td><td>42</td></tr></table></td></tr></table>")
    assert [["Financial","42"]] in parser.tables
    parser=_TableParser();parser.feed("<table><tr><td colspan='999'>x</td></tr></table>")
    assert len(parser.tables[0][0])==64
    # A wide cell must skip a column occupied by an earlier rowspan. This is
    # emitted by KAP's AVHOL/LIDFA-style taxonomy/layout tables.
    parser=_TableParser();parser.feed("<table><tr><td>A</td><td rowspan='2'>B</td><td>C</td></tr><tr><td colspan='2'>D</td></tr></table>")
    assert parser.tables[0]==[["A","B","C"],["D","B","D"]]

def test_empty_financial_envelope_is_a_valid_no_period_result(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close()
    assert KapFundamentalProvider(str(path))._parse_periods({"financials":[]})==[]

def test_page_revision_upserts_without_duplicate_periods(tmp_path):
    path=tmp_path/"kap.sqlite"; Database(str(path)).close(); clock=[NOW]; opener=FixtureOpener(fixture())
    provider=KapFundamentalProvider(str(path),opener=opener,sleeper=lambda _:None,min_request_interval=0,now=lambda:clock[0])
    provider.get_snapshot("AAA.IS",NOW)
    opener.financials=fixture().replace("<td>120.000</td>","<td>121.000</td>",1)
    opener.filing_id=9009;opener.published="16.02.2026 10:00:00"
    clock[0]=NOW+timedelta(hours=7);revised=provider.get_snapshot("AAA.IS",NOW+timedelta(hours=7))
    with Database(str(path)) as db: count=db.query("SELECT COUNT(*) n FROM kap_financial_cache WHERE symbol='AAA'")[0]["n"]
    assert count==8 and revised.periods[0].revenue.value==11_000_000
    detail_calls=len([url for url,_ in opener.calls if "/Bildirim/" in url])
    clock[0]=NOW+timedelta(hours=14);again=provider.get_snapshot("AAA.IS",NOW+timedelta(hours=14))
    assert len([url for url,_ in opener.calls if "/Bildirim/" in url])==detail_calls and again.periods[0].filing_id=="9009"
    opener.filing_id=8000;opener.published="01.01.2025 10:00:00"
    opener.financials=fixture().replace("<td>120.000</td>","<td>999.000</td>",1)
    clock[0]=NOW+timedelta(hours=21);no_rollback=provider.get_snapshot("AAA.IS",NOW+timedelta(hours=21))
    assert no_rollback.periods[0].filing_id=="9009" and no_rollback.periods[0].revenue.value==11_000_000
    with Database(str(path)) as db:
        assert db.query("SELECT COUNT(*) n FROM kap_processed_disclosures WHERE symbol='AAA'")[0]["n"]==3

def test_ytd_quarterization_consolidation_and_explicit_share_valuation(tmp_path):
    path=tmp_path/"kap.sqlite"; Database(str(path)).close()
    provider=KapFundamentalProvider(str(path),price_resolver=lambda _:(10,NOW))
    payload={"financials":[
        {"periodEnd":"2025-06-30","periodStart":"2025-01-01","currency":"TRY","unit":1,"consolidated":True,"filingId":"new","facts":{"revenue":250,"net_income":50,"net_interest_income":90,"ebitda":80,"equity":500,"total_assets":900,"outstanding_shares":100}},
        {"periodEnd":"2025-06-30","periodStart":"2025-01-01","currency":"TRY","unit":1,"consolidated":False,"filingId":"old","facts":{"revenue":1}},
        {"periodEnd":"2025-03-31","periodStart":"2025-01-01","currency":"TRY","unit":1,"consolidated":True,"filingId":"q1","facts":{"revenue":100,"net_income":20,"net_interest_income":35,"ebitda":30,"equity":480,"total_assets":850}}]}
    periods=provider._parse_periods(payload)
    assert len(periods)==2 and periods[0].consolidated and periods[0].revenue.value==150
    assert periods[0].net_interest_income.value==55
    periods[0].net_debt.value=20;periods[0].net_debt.available=True
    provider._apply_valuation("AAA.IS",periods,NOW)
    assert periods[0].pb.value==2 and periods[0].ev_ebitda.value is None  # fewer than four quarters
    periods.extend([periods[1].model_copy(update={"period_end":datetime(2024,12,31,tzinfo=timezone.utc)}),
                    periods[1].model_copy(update={"period_end":datetime(2024,9,30,tzinfo=timezone.utc)})])
    provider._apply_valuation("AAA.IS",periods,NOW)
    assert periods[0].ev_ebitda.available

def test_valuation_stays_unknown_without_validated_outstanding_shares(tmp_path):
    path=tmp_path/"kap.sqlite";Database(str(path)).close()
    provider=KapFundamentalProvider(str(path),price_resolver=lambda _:(10,NOW))
    periods=provider._parse_periods({"financials":[{"periodEnd":"2025-03-31","periodStart":"2025-01-01",
        "currency":"TRY","unit":1,"consolidated":True,"facts":{"net_income":20,"equity":100}}]})
    provider._apply_valuation("AAA.IS",periods,NOW)
    assert not periods[0].market_cap.available and not periods[0].pe.available and not periods[0].pb.available
