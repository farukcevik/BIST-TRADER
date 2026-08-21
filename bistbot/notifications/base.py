from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

log=logging.getLogger(__name__)


class NotificationProvider(Protocol):
    def send(self,event: str,message: str) -> None: ...


class SafeNotificationDispatcher:
    """Notification failures are logged and never escape into execution."""
    def __init__(self,providers: Sequence[NotificationProvider]=()): self.providers=list(providers)
    def send(self,event: str,message: str) -> None:
        for provider in self.providers:
            try: provider.send(event,message)
            except Exception: log.exception("notification provider failed",extra={"event":event})

    def system_error(self,message: str) -> None: self.send("SYSTEM ERROR",message)
    def daily_summary(self,message: str) -> None: self.send("DAILY SUMMARY",message)
