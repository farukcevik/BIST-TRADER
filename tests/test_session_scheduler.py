from dataclasses import dataclass

from bistbot.app.scheduler import Scheduler

@dataclass
class Status:
    can_execute_orders: bool

class Calendar:
    def status(self,at): return Status(at=="open")

def test_open_transition_triggers_immediate_cycle_without_waiting_regular_interval():
    calls=[]; scheduler=Scheduler(600,lambda:calls.append("cycle"),calendar=Calendar(),heartbeat_seconds=30)
    assert scheduler.heartbeat_once("closed") is False
    assert scheduler.heartbeat_once("open") is True
    assert calls==["cycle"]

def test_heartbeat_without_transition_does_not_run_expensive_cycle():
    calls=[]; scheduler=Scheduler(600,lambda:calls.append("cycle"),calendar=Calendar())
    scheduler.heartbeat_once("closed"); scheduler.heartbeat_once("closed")
    assert calls==[]
