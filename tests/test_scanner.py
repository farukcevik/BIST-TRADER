from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bistbot.app.config import load_settings
from bistbot.app.models import MarketBar
from bistbot.market.scanner import DeterministicMarketScanner
from bistbot.storage.database import Database
from bistbot.storage.repositories import TechnicalSignalRepository


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def bars(symbol: str, closes: list[float], *, base_volume: float = 200_000,
         last_volume: float | None = None, age_minutes: int = 0) -> list[MarketBar]:
    result = []
    start = NOW - timedelta(minutes=len(closes)-1+age_minutes)
    for index, close in enumerate(closes):
        volume = last_volume if index == len(closes)-1 and last_volume is not None else base_volume
        result.append(MarketBar(symbol=symbol, timestamp=start+timedelta(minutes=index), open=close*.998,
            high=close*1.005, low=close*.995, close=close, volume=volume))
    return result


def scanner(repository=None) -> DeterministicMarketScanner:
    return DeterministicMarketScanner(load_settings().scanner, repository)


def test_strong_momentum_scores_above_weak_momentum():
    strong = bars("STRONG", [100+i*.6 for i in range(30)])
    weak = bars("WEAK", [100-i*.25 for i in range(30)])
    scores = {s.symbol:s for s in scanner().scan({"STRONG":strong,"WEAK":weak}, now=NOW)}
    assert scores["STRONG"].momentum_score > scores["WEAK"].momentum_score
    assert "positive 12-bar momentum" in scores["STRONG"].reasons


def test_weak_momentum_receives_low_score():
    signal = scanner().scan({"WEAK":bars("WEAK", [120-i for i in range(30)])}, now=NOW)[0]
    assert signal.momentum_score < 35


def test_volume_spike_increases_volume_score():
    closes = [100+i*.1 for i in range(30)]
    normal = scanner().scan({"NORMAL":bars("NORMAL",closes)}, now=NOW)[0]
    spike = scanner().scan({"SPIKE":bars("SPIKE",closes,last_volume=600_000)}, now=NOW)[0]
    assert spike.volume_score > normal.volume_score
    assert "relative volume spike" in spike.reasons


def test_illiquid_symbol_is_filtered_out():
    result = scanner().scan({"ILLIQ":bars("ILLIQ",[10+i*.01 for i in range(30)],base_volume=1_000)}, now=NOW)
    assert result == []


def test_overbought_move_is_penalized_and_explained():
    closes = [100.0]*15 + [101+i*3 for i in range(15)]
    signal = scanner().scan({"HOT":bars("HOT",closes)}, now=NOW)[0]
    assert signal.metrics["rsi_14"] >= 80
    assert "overbought RSI penalty" in signal.reasons
    assert signal.technical_score < signal.momentum_score


def test_stale_data_is_rejected():
    result = scanner().scan({"STALE":bars("STALE",[100+i*.1 for i in range(30)],age_minutes=31)}, now=NOW)
    assert result == []


def test_missing_data_is_rejected():
    result = scanner().scan({"SHORT":bars("SHORT",[100+i for i in range(10)])}, now=NOW)
    assert result == []


def test_ranking_limit_tie_break_and_sqlite_recording(tmp_path):
    database = Database(str(tmp_path/"scanner.db"))
    market = {f"S{i:02d}":bars(f"S{i:02d}",[100+j*(i+1)*.03 for j in range(30)]) for i in range(45)}
    result = scanner(TechnicalSignalRepository(database)).scan(market, limit=40, now=NOW)
    assert len(result) == 40
    assert all(result[i].overall_scanner_score >= result[i+1].overall_scanner_score for i in range(39))
    assert database.query("SELECT COUNT(*) AS count FROM technical_signals")[0]["count"] == 40
    database.close()

