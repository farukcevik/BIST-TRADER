from __future__ import annotations

import hashlib
import json
import random
import re
import time
import urllib.request
import urllib.parse
import ssl
import certifi
from concurrent.futures import ThreadPoolExecutor,as_completed
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Protocol
from pathlib import Path
from zoneinfo import ZoneInfo

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
    provider_mode = "MOCK"
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
                output[key] = self._result(key, (), self.now(), 0, 1, stale_after, str(error),interval)
        for start in range(0, len(normalized), self.batch_size):
            batch = normalized[start:start+self.batch_size]
            try:
                payload, request_at, latency, attempts = self._request(batch, bars, interval)
                for symbol in batch:
                    output[symbol] = self._result(symbol, payload.get(symbol, ()), request_at, latency,
                                                  attempts, stale_after, None if symbol in payload else "symbol missing from response",interval)
            except Exception as batch_error:
                # A failed batch is retried per symbol so one bad ticker cannot discard its peers.
                for symbol in batch:
                    try:
                        payload, request_at, latency, attempts = self._request([symbol], bars, interval)
                        output[symbol] = self._result(symbol, payload.get(symbol, ()), request_at, latency,
                            attempts, stale_after, None if symbol in payload else "symbol missing from response",interval)
                    except Exception as error:
                        requested = self.now()
                        output[symbol] = self._result(symbol, (), requested, 0, self.max_attempts,
                                                      stale_after, f"{type(error).__name__}: {error}",interval)
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
                attempts: int, stale_after: timedelta, error: str | None,interval: str) -> MarketDataResult:
        try:
            candles = sorted((Candle.model_validate(item) for item in raw), key=lambda item:item.timestamp)
            if not candles: raise ValueError(error or "no market data")
            if any(item.symbol != symbol for item in candles): raise ValueError("response symbol mismatch")
            latest = candles[-1]; timestamp = _aware(latest.timestamp); current=_aware(self.now()); age = current - timestamp
            interval_end=min(current,timestamp+_interval_delta(interval))
            stale = _is_stale_for_bist_session(current,interval_end,stale_after)
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


def _is_stale_for_bist_session(now: datetime,data_timestamp: datetime,live_limit: timedelta) -> bool:
    if data_timestamp>now:return True
    zone=ZoneInfo("Europe/Istanbul"); local_now=now.astimezone(zone); local_data=data_timestamp.astimezone(zone)
    market_live=local_now.weekday()<5 and (local_now.hour,local_now.minute)>=(10,0) and (local_now.hour,local_now.minute)<(18,10)
    if market_live:return now-data_timestamp>live_limit
    expected=local_now.date()
    if (local_now.hour,local_now.minute)<(10,0) or local_now.weekday()>=5:
        expected-=timedelta(days=1)
        while expected.weekday()>=5: expected-=timedelta(days=1)
    return local_data.date()<expected


def _interval_delta(interval: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([mhd])", interval)
    if not match: raise ValueError(f"unsupported interval: {interval}")
    amount, unit = int(match.group(1)), match.group(2)
    return {"m":timedelta(minutes=amount),"h":timedelta(hours=amount),"d":timedelta(days=amount)}[unit]


class BistSymbolUniverse:
    KAP_URL="https://www.kap.org.tr/tr/api/company/items/IGS/A"
    def __init__(self,path: str|Path="data/bist_symbols.txt",*,opener=urllib.request.urlopen,timeout_seconds: float=15):
        self.path=Path(path); self.invalid_symbols=[]; self.opener=opener; self.timeout_seconds=timeout_seconds
        self.source="KAP_ACTIVE_IGS"; self.configured_count=0
    def load(self) -> list[str]:
        try:
            request=urllib.request.Request(self.KAP_URL,headers={"User-Agent":"BISTBOT/1.0","Referer":"https://www.kap.org.tr/"})
            with self.opener(request,timeout=self.timeout_seconds) as response: payload=json.load(response)
            raw=[]
            for item in payload:
                if str(item.get("payIslemDurumu")) != "1": continue
                value=str(item.get("stockCode") or "")
                raw.extend(part.strip() for part in re.split(r"[,;/ ]+",value) if part.strip())
            if not raw: raise ValueError("KAP active company list was empty")
        except Exception:
            self.source="MAINTAINED_FALLBACK"
            raw=[line.strip() for line in self.path.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
        self.configured_count=len(raw)
        valid=[]; self.invalid_symbols=[]
        for value in raw:
            symbol=value.strip().upper().removesuffix(".IS")
            if not re.fullmatch(r"[A-Z0-9]{3,6}",symbol): self.invalid_symbols.append(symbol); continue
            ticker=f"{symbol}.IS"
            if ticker not in valid: valid.append(ticker)
        return valid


class YahooChartTransport:
    """Read-only Yahoo chart transport. It has no order or broker capability."""
    def __init__(self,*,workers: int=6,retries: int=2,backoff_seconds: float=.5,
                 opener=urllib.request.urlopen,sleeper=time.sleep):
        self.workers,self.retries,self.backoff_seconds=workers,retries,backoff_seconds
        self.opener,self.sleeper=opener,sleeper
        self.ssl_context=ssl.create_default_context(cafile=certifi.where())

    def fetch(self,symbols: Sequence[str],*,bars: int,interval: str,timeout_seconds: float):
        output={}
        with ThreadPoolExecutor(max_workers=min(self.workers,max(1,len(symbols)))) as pool:
            futures={pool.submit(self._one,symbol,bars,interval,timeout_seconds):symbol for symbol in symbols}
            for future in as_completed(futures):
                try:
                    candles=future.result()
                    if candles: output[futures[future]]=candles
                except Exception: pass
        return output

    def _one(self,symbol: str,bars: int,interval: str,timeout_seconds: float) -> list[Candle]:
        # Yahoo only exposes one-minute candles over short ranges.  A one-month
        # range causes otherwise valid snapshot requests to return no result.
        range_value="5d" if interval=="1m" else "1mo" if interval.endswith(("m","h")) else "6mo"
        url=f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_value}&interval={interval}"
        last_error=None
        for attempt in range(self.retries+1):
            try:
                request=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 BISTBOT/1.0"})
                try: response=self.opener(request,timeout=timeout_seconds,context=self.ssl_context)
                except TypeError: response=self.opener(request,timeout=timeout_seconds)
                with response: payload=json.load(response)
                result=payload["chart"]["result"][0]; timestamps=result.get("timestamp",[])
                quote=result["indicators"]["quote"][0]; candles=[]
                for index,timestamp in enumerate(timestamps):
                    values=[quote.get(key,[None]*len(timestamps))[index] for key in ("open","high","low","close","volume")]
                    if any(value is None for value in values): continue
                    opening,high,low,close,volume=values
                    if min(opening,high,low,close)<=0 or volume<0: continue
                    candles.append(Candle(symbol=symbol,timestamp=datetime.fromtimestamp(timestamp,timezone.utc),
                        open=opening,high=high,low=low,close=close,volume=volume))
                return candles[-bars:]
            except Exception as error:
                last_error=error
                if attempt<self.retries: self.sleeper(self.backoff_seconds*2**attempt)
        raise last_error or RuntimeError("Yahoo response unavailable")


class YahooBistProvider(DemoMarketDataProvider):
    provider_mode = "REAL"
    def __init__(self,universe: BistSymbolUniverse|None=None,**kwargs):
        self.universe=universe or BistSymbolUniverse()
        super().__init__(YahooChartTransport(),batch_size=kwargs.pop("batch_size",30),
                         requests_per_second=kwargs.pop("requests_per_second",2),**kwargs)

    def active_symbols(self) -> list[str]: return self.universe.load()
    @property
    def invalid_symbols(self) -> list[str]: return list(self.universe.invalid_symbols)
    @property
    def symbols_configured(self) -> int: return self.universe.configured_count
    def intraday(self,symbols: Sequence[str],*,bars: int=60,interval: str="1h") -> dict[str,MarketDataResult]:
        return self._fetch(symbols,bars,interval,self.stale_after)
