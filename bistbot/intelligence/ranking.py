from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import uuid4

from bistbot.app.config import IntelligenceSettings
from bistbot.app.models import EventItem, EventSourceType, RankedEventCandidate, TechnicalSignal
from bistbot.intelligence.kap_provider import KapProvider
from bistbot.intelligence.news_provider import NewsProvider
from bistbot.storage.repositories import EventRepository, IntelligenceRankingRepository


THEMES = (
    "contract", "investment", "capacity expansion", "acquisition", "sale", "new facility",
    "regulatory approval", "financing", "debt restructuring", "capital increase", "buyback",
    "dividend", "profit warning", "guidance", "legal", "regulatory", "production disruption",
)
MATERIALITY_HINTS = re.compile(r"\b(binding|signed|board approved|million|billion|mn|bn|try|tl|usd|eur|%|halted|suspended)\b", re.I)


def event_hash(symbol: str, source: str, title: str, body: str) -> str:
    normalized = " ".join(f"{symbol}|{source}|{title}|{body}".lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def make_event(*, symbol: str, source: str, source_type: EventSourceType, title: str,
               published_at: datetime, fetched_at: datetime, body: str = "", url: str | None = None,
               trust_score: float) -> EventItem:
    digest = event_hash(symbol, source, title, body)
    return EventItem(id=digest[:24], symbol=symbol, source=source, source_type=source_type,
        title=title, body=body, url=url, published_at=published_at, fetched_at=fetched_at,
        hash=digest, trust_score=trust_score)


class EventRanker:
    """Deterministic pre-LLM enrichment; produces no directional trading opinion."""
    def __init__(self, settings: IntelligenceSettings, news: NewsProvider, kap: KapProvider,
                 event_repository: EventRepository | None = None,
                 ranking_repository: IntelligenceRankingRepository | None = None):
        self.settings, self.news, self.kap = settings, news, kap
        self.event_repository, self.ranking_repository = event_repository, ranking_repository

    def rank(self, candidates: Sequence[TechnicalSignal], *, limit: int = 10,
             now: datetime | None = None) -> list[RankedEventCandidate]:
        if limit <= 0: raise ValueError("limit must be positive")
        now = now or datetime.now(timezone.utc); symbols = [candidate.symbol for candidate in candidates]
        events, errors = [], []
        for name, provider in (("NEWS", self.news), ("KAP", self.kap)):
            fetched, provider_errors = self._safe_fetch(name, provider, symbols)
            events.extend(fetched); errors.extend(provider_errors)
        events = [event.model_copy(update={"trust_score": self.settings.kap_trust_score
                  if event.source_type is EventSourceType.KAP else self.settings.news_trust_score}) for event in events]
        events = self._deduplicate(events, set(symbols))
        if self.event_repository:
            for event in events: self.event_repository.add(event)
        by_symbol: dict[str, list[EventItem]] = {symbol:[] for symbol in symbols}
        for event in events: by_symbol[event.symbol].append(event)
        any_events = bool(events); ranked = []
        for candidate in candidates:
            symbol_events = by_symbol[candidate.symbol]
            scores = [self._score_event(event, candidate.symbol, now) for event in symbol_events]
            event_score = max(scores) if scores else 0.0
            if len(scores) > 1: event_score = min(100.0, event_score*.8 + sum(scores)/len(scores)*.2)
            combined = (candidate.overall_scanner_score if not any_events else
                        candidate.overall_scanner_score*self.settings.scanner_weight + event_score*self.settings.event_weight)
            reasons = ["external providers unavailable; scanner rank preserved"] if not any_events and errors else []
            if symbol_events: reasons.append(f"{len(symbol_events)} unique event(s)")
            ranked.append(RankedEventCandidate(symbol=candidate.symbol, scanner_score=candidate.overall_scanner_score,
                event_score=round(event_score,4), combined_score=round(combined,4),
                event_ids=[event.id for event in sorted(symbol_events,key=lambda item:item.published_at,reverse=True)], reasons=reasons))
        ranked.sort(key=lambda item:(-item.combined_score,item.symbol)); selected = ranked[:limit]
        if self.ranking_repository:
            cycle_id = uuid4().hex
            for rank, item in enumerate(selected,1): self.ranking_repository.add(cycle_id, rank, item, now)
            for source, reason in errors: self.ranking_repository.add_error(cycle_id, source, reason, now)
        return selected

    def _safe_fetch(self, name: str, provider: NewsProvider | KapProvider, symbols: list[str]):
        try: return provider.fetch(symbols), []
        except Exception as batch_error:
            events, errors = [], []
            for symbol in symbols:
                try: events.extend(provider.fetch([symbol]))
                except Exception as error: errors.append((name, f"{symbol}: {type(error).__name__}: {error}"))
            if not symbols: errors.append((name, f"{type(batch_error).__name__}: {batch_error}"))
            return events, errors

    @staticmethod
    def _deduplicate(events: Sequence[EventItem], candidates: set[str]) -> list[EventItem]:
        unique: dict[str, EventItem] = {}
        for event in events:
            if event.symbol not in candidates: continue
            existing = unique.get(event.hash)
            if existing is None or (event.trust_score,event.fetched_at) > (existing.trust_score,existing.fetched_at):
                unique[event.hash] = event
        return sorted(unique.values(),key=lambda item:(item.symbol,item.hash))

    def _score_event(self, event: EventItem, symbol: str, now: datetime) -> float:
        age_hours = max(0.0, (_aware(now)-_aware(event.published_at)).total_seconds()/3600)
        recency = 100 * .5**(age_hours/self.settings.recency_half_life_hours)
        text = f"{event.title} {event.body}".lower(); symbol_token = symbol.removesuffix(".IS").lower()
        relevance = min(100.0, 70 + text.count(symbol_token)*15)
        matches = sum(1 for theme in THEMES if theme in text)
        keywords = min(100.0, matches*35)
        hints = len(MATERIALITY_HINTS.findall(text)); materiality = min(100.0, hints*30 + (20 if len(event.body)>300 else 0))
        w = self.settings.event_score_weights
        return min(100.0, recency*w.recency + event.trust_score*w.source_trust +
                   relevance*w.symbol_relevance + keywords*w.keywords + materiality*w.materiality)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
