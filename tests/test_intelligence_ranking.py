from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bistbot.app.config import load_settings
from bistbot.app.models import EventSourceType, TechnicalSignal
from bistbot.intelligence.kap_provider import MockKapProvider
from bistbot.intelligence.news_provider import MockNewsProvider
from bistbot.intelligence.ranking import EventRanker, make_event
from bistbot.storage.database import Database
from bistbot.storage.repositories import EventRepository, IntelligenceRankingRepository


NOW = datetime(2026,8,21,12,0,tzinfo=timezone.utc)


def candidate(symbol: str, score: float) -> TechnicalSignal:
    return TechnicalSignal(symbol=symbol,timestamp=NOW,technical_score=score,momentum_score=score,
        volume_score=score,liquidity_score=score,volatility_score=score,overall_scanner_score=score,
        metrics={},reasons=[])


def event(symbol: str, source_type: EventSourceType, title: str, *, age_hours: int = 1, body: str = ""):
    return make_event(symbol=symbol,source="KAP" if source_type is EventSourceType.KAP else "Wire",
        source_type=source_type,title=title,body=body,published_at=NOW-timedelta(hours=age_hours),
        fetched_at=NOW,trust_score=1)


def ranker(news=(),kap=(),news_error=None,kap_error=None,repositories=(None,None)):
    return EventRanker(load_settings().intelligence,MockNewsProvider(news,news_error),MockKapProvider(kap,kap_error),
                       repositories[0],repositories[1])


def test_kap_receives_higher_trust_than_generic_news():
    kap = event("KAPCO",EventSourceType.KAP,"routine update")
    news = event("NEWSCO",EventSourceType.NEWS,"routine update")
    results = {item.symbol:item for item in ranker([news],[kap]).rank([candidate("KAPCO",50),candidate("NEWSCO",50)],now=NOW)}
    assert results["KAPCO"].event_score > results["NEWSCO"].event_score


def test_recent_material_event_scores_above_old_generic_event():
    strong = event("AAA",EventSourceType.KAP,"Binding acquisition and new facility contract",
                   body="Board approved TRY 2 billion capacity expansion")
    weak = event("BBB",EventSourceType.NEWS,"Company mention",age_hours=96)
    results = {item.symbol:item for item in ranker([weak],[strong]).rank([candidate("AAA",50),candidate("BBB",50)],now=NOW)}
    assert results["AAA"].event_score > results["BBB"].event_score


def test_duplicate_events_are_collapsed():
    first = event("AAA",EventSourceType.NEWS,"Dividend guidance")
    duplicate = first.model_copy(update={"id":"different-id"})
    result = ranker([first,duplicate]).rank([candidate("AAA",50)],now=NOW)[0]
    assert len(result.event_ids) == 1


def test_provider_unavailability_preserves_scanner_ranking():
    candidates = [candidate("HIGH",90),candidate("LOW",20)]
    result = ranker(news_error=TimeoutError("down"),kap_error=ConnectionError("down")).rank(candidates,now=NOW)
    assert [item.symbol for item in result] == ["HIGH","LOW"]
    assert result[0].combined_score == 90
    assert "scanner rank preserved" in result[0].reasons[0]


def test_one_provider_can_fail_while_other_enriches():
    disclosure = event("AAA",EventSourceType.KAP,"Regulatory approval")
    result = ranker(kap=[disclosure],news_error=TimeoutError("down")).rank([candidate("AAA",50)],now=NOW)[0]
    assert result.event_score > 0 and result.event_ids == [disclosure.id]


def test_returns_top_ten_with_deterministic_ranking():
    candidates = [candidate(f"S{i:02d}",float(i)) for i in range(40)]
    result = ranker().rank(candidates,limit=10,now=NOW)
    assert len(result) == 10
    assert [item.symbol for item in result] == [f"S{i:02d}" for i in range(39,29,-1)]


def test_persists_deduplicated_events_rankings_and_errors(tmp_path):
    database = Database(str(tmp_path/"events.db"))
    repositories = EventRepository(database), IntelligenceRankingRepository(database)
    disclosure = event("AAA",EventSourceType.KAP,"Capital increase",body="Board approved TRY 100 million")
    ranker(kap=[disclosure],news_error=TimeoutError("down"),repositories=repositories).rank([candidate("AAA",50)],now=NOW)
    assert database.query("SELECT COUNT(*) n FROM event_items")[0]["n"] == 1
    assert database.query("SELECT COUNT(*) n FROM intelligence_rankings")[0]["n"] == 1
    assert database.query("SELECT COUNT(*) n FROM system_events WHERE event_type='INTELLIGENCE_PROVIDER_ERROR'")[0]["n"] >= 1
    database.close()

