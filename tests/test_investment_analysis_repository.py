from datetime import datetime, timezone

from pydantic import BaseModel

from bistbot.fundamental import FundamentalAssessment, FundamentalProviderStatus, FundamentalSnapshot
from bistbot.storage.database import Database
from bistbot.storage.repositories import InvestmentAnalysisRepository
from bistbot.strategy.levels import TechnicalLevels
from bistbot.strategy.potential import assess_potential


NOW = datetime(2026, 9, 9, 10, tzinfo=timezone.utc)


class Levels(BaseModel):
    symbol: str
    timestamp: datetime
    confidence: float


class Potential(BaseModel):
    potential_score: float | None
    target_confidence: float


def test_analysis_artifacts_are_append_only(tmp_path):
    database = Database(str(tmp_path / "analysis.db"))
    repository = InvestmentAnalysisRepository(database)
    snapshot = FundamentalSnapshot(symbol="AAA.IS", as_of=NOW, source="fixture",
        provider_status=FundamentalProviderStatus.UNAVAILABLE)
    score = FundamentalAssessment(symbol="AAA.IS", as_of=NOW,
        provider_status=FundamentalProviderStatus.UNAVAILABLE, confidence=0)

    repository.add_fundamental_snapshot(snapshot)
    repository.add_fundamental_snapshot(snapshot)
    repository.add_fundamental_score(score)
    repository.add_technical_levels(Levels(symbol="AAA.IS", timestamp=NOW, confidence=70))
    repository.add_potential_assessment(Potential(potential_score=None, target_confidence=0),
        symbol="AAA.IS", timestamp=NOW)

    assert database.query("SELECT COUNT(*) n FROM fundamental_snapshots")[0]["n"] == 2
    assert database.query("SELECT score,provider_status FROM fundamental_scores")[0]["score"] is None
    assert database.query("SELECT COUNT(*) n FROM technical_levels")[0]["n"] == 1
    assert database.query("SELECT score FROM potential_assessments")[0]["score"] is None
    database.close()


def test_unavailable_potential_persists_derived_confidence(tmp_path):
    database=Database(str(tmp_path/"unavailable-potential.db"))
    repository=InvestmentAnalysisRepository(database)
    levels=TechnicalLevels(symbol="AAA.IS",timestamp=NOW,confidence=70)
    assessment=assess_potential(None,95,levels,2,80,75,1.2,85,None,0)

    assert not assessment.available and assessment.target_confidence>0
    repository.add_potential_assessment(assessment)

    row=database.query("SELECT confidence,payload FROM potential_assessments")[0]
    assert row["confidence"]==assessment.target_confidence
    assert '"target_confidence": '+str(assessment.target_confidence) in row["payload"]
    database.close()
