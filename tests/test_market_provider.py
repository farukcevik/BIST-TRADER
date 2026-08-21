from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bistbot.app.models import Candle
from bistbot.market.provider import DemoMarketDataProvider


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


class FakeTime:
    def __init__(self): self.value = 0.0; self.sleeps: list[float] = []
    def clock(self) -> float: return self.value
    def sleep(self, seconds: float) -> None: self.sleeps.append(seconds); self.value += seconds


def candle(symbol: str, timestamp: datetime = NOW) -> Candle:
    return Candle(symbol=symbol, timestamp=timestamp, open=99, high=102, low=98, close=100, volume=200_000)


class RecordingTransport:
    def __init__(self): self.calls = []; self.failures = 0
    def fetch(self, symbols, *, bars, interval, timeout_seconds):
        self.calls.append((list(symbols), bars, interval, timeout_seconds))
        return {symbol:[candle(symbol)] for symbol in symbols}


def provider(transport, fake=None, **kwargs):
    fake = fake or FakeTime()
    kwargs.setdefault("requests_per_second", 10_000)
    kwargs.setdefault("backoff_seconds", .1)
    return DemoMarketDataProvider(transport, now=lambda:NOW, clock=fake.clock, sleeper=fake.sleep,
        **kwargs)


def test_bist_symbols_batch_fetch_and_normalization():
    transport = RecordingTransport(); data = provider(transport, batch_size=2).intraday(["THYAO", "GARAN.IS", "ASELS"])
    assert set(data) == {"THYAO.IS","GARAN.IS","ASELS.IS"}
    assert len(transport.calls) == 2
    assert all(result.available for result in data.values())


def test_historical_ohlcv_and_diagnostics():
    transport = RecordingTransport(); result = provider(transport).historical(["THYAO"], bars=30)["THYAO.IS"]
    assert result.snapshot.price == 100
    assert result.diagnostics.data_timestamp == NOW
    assert result.diagnostics.request_timestamp == NOW
    assert result.diagnostics.latency_ms >= 0
    assert result.diagnostics.error_reason is None
    assert transport.calls[0][1:] == (30,"1d",10)


def test_stale_data_is_explicitly_unavailable():
    class StaleTransport:
        def fetch(self, symbols, **kwargs): return {symbols[0]:[candle(symbols[0],NOW-timedelta(hours=1))]}
    result = provider(StaleTransport(), stale_after=timedelta(minutes=30)).intraday(["THYAO"])["THYAO.IS"]
    assert result.available is False
    assert result.snapshot is not None and result.snapshot.is_stale
    assert result.diagnostics.is_stale and "stale data" in result.diagnostics.error_reason


def test_retry_exponential_backoff_and_timeout_forwarding():
    fake = FakeTime()
    class FlakyTransport:
        def __init__(self): self.calls = 0; self.timeouts = []
        def fetch(self, symbols, *, timeout_seconds, **kwargs):
            self.calls += 1; self.timeouts.append(timeout_seconds)
            if self.calls < 3: raise TimeoutError("temporary")
            return {symbols[0]:[candle(symbols[0])]}
    transport = FlakyTransport(); result = provider(transport,fake,max_attempts=3,timeout_seconds=2).snapshots(["THYAO"])["THYAO.IS"]
    assert result.available and result.diagnostics.attempts == 3
    assert transport.timeouts == [2,2,2]
    assert .1 in fake.sleeps and .2 in fake.sleeps


def test_one_symbol_failure_does_not_fail_batch():
    class IsolatingTransport:
        def fetch(self, symbols, **kwargs):
            if "BAD.IS" in symbols: raise ConnectionError("unavailable")
            return {symbol:[candle(symbol)] for symbol in symbols}
    results = provider(IsolatingTransport(),max_attempts=2).snapshots(["GOOD","BAD"])
    assert results["GOOD.IS"].available
    assert not results["BAD.IS"].available
    assert "ConnectionError" in results["BAD.IS"].diagnostics.error_reason


def test_missing_symbol_response_is_diagnostic_not_exception():
    class EmptyTransport:
        def fetch(self, symbols, **kwargs): return {}
    result = provider(EmptyTransport()).snapshots(["THYAO"])["THYAO.IS"]
    assert not result.available
    assert "symbol missing" in result.diagnostics.error_reason


def test_invalid_symbol_does_not_block_valid_symbol():
    results = provider(RecordingTransport()).snapshots(["THYAO", "not valid!"])
    assert results["THYAO.IS"].available
    assert not results["NOT VALID!"].available
    assert "invalid BIST symbol" in results["NOT VALID!"].diagnostics.error_reason


def test_rate_limiting_waits_between_batches():
    fake = FakeTime(); transport = RecordingTransport()
    provider(transport,fake,batch_size=1,requests_per_second=2).snapshots(["AAA","BBB"])
    assert any(delay >= .5 for delay in fake.sleeps)


def test_demo_universe_and_intraday_are_deterministic():
    demo = DemoMarketDataProvider(now=lambda:NOW, requests_per_second=1_000_000)
    assert len(demo.active_symbols()) == 523
    first = demo.intraday(["THYAO"],bars=3)["THYAO.IS"]
    second = demo.intraday(["THYAO"],bars=3)["THYAO.IS"]
    assert [bar.model_dump() for bar in first.candles] == [bar.model_dump() for bar in second.candles]
