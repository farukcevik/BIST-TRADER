from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from threading import Event

log = logging.getLogger(__name__)


class Scheduler:
    """Full-cycle scheduler with a lightweight session-transition heartbeat."""
    def __init__(self,interval_seconds:int,cycle:Callable[[],None],*,calendar=None,heartbeat_seconds:int=60,
                 monotonic:Callable[[],float]=time.monotonic,now:Callable[[],datetime]=datetime.now):
        self.interval_seconds,self.cycle=interval_seconds,cycle
        self.calendar,self.heartbeat_seconds,self.monotonic,self.now=calendar,heartbeat_seconds,monotonic,now
        self._last_full=None; self._previous_executable=None

    def heartbeat_once(self,at:datetime|None=None)->bool:
        """Check only session state; return True when an immediate full cycle ran."""
        if self.calendar is None:return False
        executable=self.calendar.status(at or self.now()).can_execute_orders
        opened=self._previous_executable is False and executable is True
        self._previous_executable=executable
        if opened:
            self.cycle(); self._last_full=self.monotonic(); return True
        return False

    def run_forever(self, stop: Event | None = None) -> None:
        stop = stop or Event()
        while not stop.is_set():
            try:
                elapsed=float("inf") if self._last_full is None else self.monotonic()-self._last_full
                if elapsed>=self.interval_seconds:
                    self.cycle(); self._last_full=self.monotonic()
                    if self.calendar is not None:self._previous_executable=self.calendar.status(self.now()).can_execute_orders
                else:self.heartbeat_once()
            except Exception:
                log.exception("scheduled cycle failed safely")
            stop.wait(self.heartbeat_seconds if self.calendar is not None else self.interval_seconds)

    def run_once(self) -> None:
        self.cycle()
