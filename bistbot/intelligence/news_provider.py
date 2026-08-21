from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from bistbot.app.models import EventItem


class NewsProvider(Protocol):
    def fetch(self, symbols: Sequence[str]) -> list[EventItem]: ...


class MockNewsProvider:
    provider_mode = "MOCK"
    def __init__(self, events: Sequence[EventItem] = (), error: Exception | None = None):
        self.events, self.error = list(events), error

    def fetch(self, symbols: Sequence[str]) -> list[EventItem]:
        if self.error: raise self.error
        wanted = set(symbols)
        return [event for event in self.events if event.symbol in wanted]


class DisabledNewsProvider:
    provider_mode = "DISABLED"
    def fetch(self,symbols: Sequence[str]) -> list[EventItem]: return []
