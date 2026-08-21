from __future__ import annotations

import hashlib
import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Protocol

from bistbot.app.models import Candle, MarketDataResult, MarketSnapshot, ProviderDiagnostics


class MarketDataProvider(Protocol):
    def active_symbols(self) -> list[str]: ...
    def historical(self, symbols: Sequence[str], *, bars: int = 60, interval: str = "1d") -> dict[str, MarketDataResult]: ...
    def intraday(self, symbols: Sequence[str], *, bars: int = 60, interval: str = "5m") -> dict[str, MarketDataResult]: ...
    def snapshots(self, symbols: Sequence[str]) -> dict[str, MarketDataResult]: ...


class MarketDataTransport(Protocol):
    def fetch(self, symbols: Sequence[str], *, bars: int, interval: str,
              timeout_seconds: float) -> Mapping[str, Sequence[Candle]]: ...


class RateLimiter:
    def __init__(self, requests_per_second: float, *, clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep):
        if requests_per_second <= 0: raise ValueError("requests_per_second must be positive")
        self.minimum_interval = 1 / requests_per_second
        self.clock, self.sleeper, self._last_request = clock, sleeper, None

    def wait(self) -> None:
        now = self.clock()
        if self._last_request is not None:
            remaining = self.minimum_interval - (now - self._last_request)
            if remaining > 0: self.sleeper(remaining)
        self._last_request = self.clock()


class DemoTransport:
    """Deterministic, credential-free OHLCV source for V1 paper-trading exercises."""
    def __init__(self, now: Callable[[], datetime] | None = None):
        self.now = now or (lambda: datetime.now(timezone.utc))

    def fetch(self, symbols: Sequence[str], *, bars: int, interval: str,
              timeout_seconds: float) -> Mapping[str, Sequence[Candle]]:
        if timeout_seconds <= 0: raise TimeoutError("timeout must be positive")
        spacing = _interval_delta(interval); end = self.now()
        output: dict[str, list[Candle]] = {}
        for symbol in symbols:
            seed = int(hashlib.sha256(f"{symbol}:{interval}".encode()).hexdigest()[:16], 16)
            rng = random.Random(seed); price = rng.uniform(15, 350); candles = []
            for index in range(bars):
                drift = rng.uniform(-.018, .021); opening = price; close = max(.01, opening*(1+drift))
                high = max(opening, close)*(1+rng.uniform(0,.008)); low = min(opening, close)*(1-rng.uniform(0,.008))
                volume = rng.uniform(150_000, 4_000_000)
                candles.append(Candle(symbol=symbol, timestamp=end-spacing*(bars-1-index), open=opening,
                                      high=high, low=low, close=close, volume=volume)); price = close
            output[symbol] = candles
        return output


class DemoMarketDataProvider:
    SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,11}(?:\.IS)?$")

    def __init__(self, transport: MarketDataTransport | None = None, *, timeout_seconds: float = 10,
                 max_attempts: int = 3, backoff_seconds: float = .25, requests_per_second: float = 5,
                 batch_size: int = 50, stale_after: timedelta = timedelta(minutes=30),
                 now: Callable[[], datetime] | None = None, clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep):
        if timeout_seconds <= 0 or max_attempts <= 0 or batch_size <= 0: raise ValueError("invalid provider configuration")
        self.now = now or (lambda: datetime.now(timezone.utc)); self.transport = transport or DemoTransport(self.now)
        self.timeout_seconds, self.max_attempts, self.backoff_seconds = timeout_seconds, max_attempts, backoff_seconds
        self.batch_size, self.stale_after, self.clock, self.sleeper = batch_size, stale_after, clock, sleeper
        self.rate_limiter = RateLimiter(requests_per_second, clock=clock, sleeper=sleeper)

    def active_symbols(self) -> list[str]:
        return [f"BIST{index:03d}.IS" for index in range(1, 524)]

    def historical(self, symbols: Sequence[str], *, bars: int = 60, interval: str = "1d") -> dict[str, MarketDataResult]:
        return self._fetch(symbols, bars, interval, timedelta(days=3))

    def intraday(self, symbols: Sequence[str], *, bars: int = 60, interval: str = "5m") -> dict[str, MarketDataResult]:
        return self._fetch(symbols, bars, interval, self.stale_after)

    def snapshots(self, symbols: Sequence[str]) -> dict[str, MarketDataResult]:
        return self._fetch(symbols, 1, "1m", self.stale_after)

    def _fetch(self, symbols: Sequence[str], bars: int, interval: str,
               stale_after: timedelta) -> dict[str, MarketDataResult]:
        if bars <= 0: raise ValueError("bars must be positive")
        output: dict[str, MarketDataResult] = {}
        normalized = []
        for raw_symbol in symbols:
            try:
                symbol = self._normalize_symbol(raw_symbol)
                if symbol not in normalized: normalized.append(symbol)
            except ValueError as error:
                key = raw_symbol.strip().upper() or raw_symbol
                output[key] = self._result(key, (), self.now(), 0, 1, stale_after, str(error))
        for start in range(0, len(normalized), self.batch_size):
            batch = normalized[start:start+self.batch_size]
            try:
                payload, request_at, latency, attempts = self._request(batch, bars, interval)
                for symbol in batch:
                    output[symbol] = self._result(symbol, payload.get(symbol, ()), request_at, latency,
                                                  attempts, stale_after, None if symbol in payload else "symbol missing from response")
            except Exception as batch_error:
                # A failed batch is retried per symbol so one bad ticker cannot discard its peers.
                for symbol in batch:
                    try:
                        payload, request_at, latency, attempts = self._request([symbol], bars, interval)
                        output[symbol] = self._result(symbol, payload.get(symbol, ()), request_at, latency,
                            attempts, stale_after, None if symbol in payload else "symbol missing from response")
                    except Exception as error:
                        requested = self.now()
                        output[symbol] = self._result(symbol, (), requested, 0, self.max_attempts,
                                                      stale_after, f"{type(error).__name__}: {error}")
        return output

    def _request(self, symbols: Sequence[str], bars: int, interval: str):
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts+1):
            self.rate_limiter.wait(); request_at = self.now(); started = self.clock()
            try:
                payload = self.transport.fetch(symbols, bars=bars, interval=interval,
                                               timeout_seconds=self.timeout_seconds)
                return payload, request_at, max(0, (self.clock()-started)*1000), attempt
            except Exception as error:
                last_error = error
                if attempt < self.max_attempts: self.sleeper(self.backoff_seconds * 2**(attempt-1))
        assert last_error is not None
        raise last_error

    def _result(self, symbol: str, raw: Sequence[Candle], request_at: datetime, latency_ms: float,
                attempts: int, stale_after: timedelta, error: str | None) -> MarketDataResult:
        try:
            candles = sorted((Candle.model_validate(item) for item in raw), key=lambda item:item.timestamp)
            if not candles: raise ValueError(error or "no market data")
            if any(item.symbol != symbol for item in candles): raise ValueError("response symbol mismatch")
            latest = candles[-1]; timestamp = _aware(latest.timestamp); age = _aware(self.now()) - timestamp
            stale = age < timedelta(0) or age > stale_after
            reason = error or (f"stale data: age={age}" if stale else None)
            snapshot = MarketSnapshot(symbol=symbol, timestamp=latest.timestamp, price=latest.close,
                volume=latest.volume, fields={"open":latest.open,"high":latest.high,"low":latest.low,"close":latest.close},
                is_stale=stale)
            diagnostics = ProviderDiagnostics(symbol=symbol, request_timestamp=request_at,
                data_timestamp=latest.timestamp, latency_ms=latency_ms, is_stale=stale,
                error_reason=reason, attempts=attempts)
            return MarketDataResult(symbol=symbol, candles=candles, snapshot=snapshot, diagnostics=diagnostics)
        except Exception as failure:
            diagnostics = ProviderDiagnostics(symbol=symbol, request_timestamp=request_at, latency_ms=latency_ms,
                is_stale=True, error_reason=f"{type(failure).__name__}: {failure}", attempts=attempts)
            return MarketDataResult(symbol=symbol, diagnostics=diagnostics)

    @classmethod
    def _normalize_symbol(cls, symbol: str) -> str:
        normalized = symbol.strip().upper()
        if not cls.SYMBOL_PATTERN.fullmatch(normalized): raise ValueError(f"invalid BIST symbol: {symbol!r}")
        return normalized if normalized.endswith(".IS") else f"{normalized}.IS"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _interval_delta(interval: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([mhd])", interval)
    if not match: raise ValueError(f"unsupported interval: {interval}")
    amount, unit = int(match.group(1)), match.group(2)
    return {"m":timedelta(minutes=amount),"h":timedelta(hours=amount),"d":timedelta(days=amount)}[unit]
