from __future__ import annotations

from datetime import datetime,timedelta,timezone
from io import BytesIO
import json
from urllib.error import HTTPError

from bistbot.intelligence.kap_provider import RealKapProvider
from bistbot.storage.database import Database
from bistbot.storage.repositories import EventRepository
from bistbot.intelligence.ranking import make_event
from bistbot.app.models import EventSourceType


class Response(BytesIO):
    status=200
    headers={}
    def __enter__(self): return self
    def __exit__(self,*args): self.close()


def disclosure(index,published="18.09.2026 10:00:00"):
    return {"publishDate":published,"summary":"Yeni sözleşme","kapTitle":"Şirket",
        "stockCodes":"THYAO","relatedStocks":None,"disclosureIndex":index}


def test_429_cooldown_recovery_backfills_and_deduplicates(tmp_path):
    database=Database(str(tmp_path/"kap.db")); current=[datetime(2026,9,18,8,tzinfo=timezone.utc)]
    calls=[]; replies=[Response(json.dumps([disclosure(100)]).encode()),
        HTTPError("https://kap.test",429,"limited",{"Retry-After":"120"},None),
        Response(json.dumps([disclosure(100),disclosure(101,"18.09.2026 10:03:00")]).encode())]
    def opener(request,timeout):
        calls.append(json.loads(request.data)); reply=replies.pop(0)
        if isinstance(reply,Exception): raise reply
        return reply
    provider=RealKapProvider(database=database,opener=opener,now=lambda:current[0],max_attempts=3,
        sleeper=lambda _:(_ for _ in ()).throw(AssertionError("429 must not retry")),jitter=lambda a,b:0)

    assert [event.id for event in provider.fetch(["THYAO.IS"])]==["kap:100:THYAO"]
    cursor=database.query("SELECT cursor_published_at FROM kap_disclosure_poll_state")[0][0]
    current[0]+=timedelta(minutes=1)
    assert [event.id for event in provider.fetch(["THYAO.IS"])]==["kap:100:THYAO"]
    assert provider.availability=="UNAVAILABLE" and provider.last_diagnostics.cooldown_until==current[0]+timedelta(seconds=120)
    assert database.query("SELECT cursor_published_at FROM kap_disclosure_poll_state")[0][0]==cursor

    current[0]+=timedelta(seconds=30)
    provider.fetch(["THYAO.IS"])
    assert len(calls)==2  # cooldown is local and never creates another KAP request
    current[0]+=timedelta(seconds=91)
    events=provider.fetch(["THYAO.IS"])
    assert {event.id for event in events}=={"kap:100:THYAO","kap:101:THYAO"}
    assert len(database.query("SELECT * FROM kap_disclosures_raw"))==2
    assert calls[2]["fromDate"]==datetime.fromisoformat(cursor).date().isoformat()
    state=database.query("SELECT * FROM kap_disclosure_poll_state")[0]
    assert state["consecutive_failures"]==0 and state["cursor_published_at"]>cursor
    database.close()


def test_retention_is_transactional_idempotent_and_preserves_financial_and_cursor_state(tmp_path):
    database=Database(str(tmp_path/"kap.db")); now=datetime(2026,9,18,8,tzinfo=timezone.utc)
    old=(now-timedelta(days=91)).isoformat()
    database.execute("INSERT INTO kap_disclosures_raw VALUES(?,?,?,?)",("old",old,old,json.dumps(disclosure("old","19.06.2026 10:00:00"))))
    database.execute("INSERT INTO event_items VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("old-event","THYAO.IS","KAP","KAP","old","","",old,old,"old-hash",95))
    database.execute("INSERT INTO kap_financial_cache VALUES(?,?,?,?,?,?,?,?)",
        ("THYAO.IS","2026-06-30",1,"filing",old,"hash","{}",old))
    database.execute("INSERT INTO kap_processed_catalysts VALUES(?,?,?,?)",("old-event",old,old,"{}"))
    provider=RealKapProvider(database=database,opener=lambda *a,**k:Response(b"[]"),now=lambda:now)
    provider.fetch(["THYAO.IS"]); provider.fetch(["THYAO.IS"])
    assert database.query("SELECT * FROM kap_disclosures_raw")==[]
    assert database.query("SELECT * FROM event_items WHERE source_type='KAP'")==[]
    assert database.query("SELECT * FROM kap_processed_catalysts")==[]
    assert len(database.query("SELECT * FROM kap_financial_cache"))==1
    assert len(database.query("SELECT * FROM kap_disclosure_poll_state"))==1
    assert provider.last_diagnostics.cleanup_deleted==0  # second cleanup is idempotent
    database.close()


def test_global_sqlite_lease_prevents_overlapping_cross_instance_requests_and_recovers_after_expiry(tmp_path):
    path=tmp_path/"kap.db"; first_db=Database(str(path)); second_db=Database(str(path))
    now=datetime(2026,9,18,8,tzinfo=timezone.utc); calls=[]; second_results=[]
    second=RealKapProvider(database=second_db,opener=lambda *a,**k:(_ for _ in ()).throw(
        AssertionError("non-owner must not call KAP")),now=lambda:now)
    def first_opener(request,timeout):
        calls.append("first")
        second_results.extend(second.fetch(["THYAO.IS"]))
        return Response(json.dumps([disclosure(200)]).encode())
    first=RealKapProvider(database=first_db,opener=first_opener,now=lambda:now)
    assert [event.id for event in first.fetch(["THYAO.IS"])]==["kap:200:THYAO"]
    assert calls==["first"] and second_results==[]
    assert second.availability=="UNAVAILABLE" and second.last_diagnostics.error_type=="KAP_POLL_IN_PROGRESS"

    expired=(now-timedelta(seconds=1)).isoformat()
    second_db.execute("UPDATE kap_disclosure_poll_state SET lease_owner='crashed',lease_expires_at=?",(expired,))
    recovered_calls=[]
    recovered=RealKapProvider(database=second_db,opener=lambda *a,**k:(recovered_calls.append(1) or Response(b"[]")),now=lambda:now)
    recovered.fetch(["THYAO.IS"])
    assert recovered_calls==[1]
    state=second_db.query("SELECT lease_owner,lease_expires_at FROM kap_disclosure_poll_state")[0]
    assert state["lease_owner"] is None and state["lease_expires_at"] is None
    first_db.close();second_db.close()


def test_processed_kap_catalyst_history_is_deduplicated_and_non_kap_history_is_not_added(tmp_path):
    database=Database(str(tmp_path/"kap.db")); now=datetime(2026,9,18,8,tzinfo=timezone.utc)
    repository=EventRepository(database)
    kap=make_event(symbol="THYAO.IS",source="KAP",source_type=EventSourceType.KAP,title="Contract",
        published_at=now,fetched_at=now,trust_score=95,source_id="kap:300:THYAO")
    news=make_event(symbol="THYAO.IS",source="Wire",source_type=EventSourceType.NEWS,title="News",
        published_at=now,fetched_at=now,trust_score=65,source_id="news:1")
    repository.add(kap);repository.add(kap);repository.add(news)
    rows=database.query("SELECT event_id,payload FROM kap_processed_catalysts")
    assert len(rows)==1 and rows[0]["event_id"]==kap.id and json.loads(rows[0]["payload"])["source_type"]=="KAP"
    database.close()
