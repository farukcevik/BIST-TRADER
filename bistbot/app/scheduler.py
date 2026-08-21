from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Event

log = logging.getLogger(__name__)


class Scheduler:
    """Small process-local scheduler; cycle owns market-hours decisions."""
    def __init__(self, interval_seconds: int, cycle: Callable[[], None]):
        self.interval_seconds, self.cycle = interval_seconds, cycle

    def run_forever(self, stop: Event | None = None) -> None:
        stop = stop or Event()
        while not stop.is_set():
            try:
                self.cycle()
            except Exception:
                log.exception("scheduled cycle failed safely")
            stop.wait(self.interval_seconds)

    def run_once(self) -> None:
        self.cycle()
