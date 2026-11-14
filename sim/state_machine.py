"""Generator run-state machine: Off -> Crank -> Run -> Stop -> Off, with Fault lockout.

Mirrors the firmware's 5-state interlock, with one deliberate difference: crank and
stop are background timers, so a command reply goes back immediately instead of
after the 2.5 s / 3 s relay pulse (the firmware blocks inside its method handler).

Run-state codes match telemetry field `run`: 0=Off, 1=Crank, 2=Run, 3=Stop, 4=Fault.
"""

from __future__ import annotations

import logging
import threading
from enum import IntEnum
from typing import Callable

log = logging.getLogger(__name__)


class RunState(IntEnum):
    OFF = 0
    CRANK = 1
    RUN = 2
    STOP = 3
    FAULT = 4


STATE_NAMES = {
    RunState.OFF: "OFF",
    RunState.CRANK: "CRANKING",
    RunState.RUN: "RUNNING",
    RunState.STOP: "STOPPING",
    RunState.FAULT: "FAULT",
}

# Fault bitmask (telemetry field `flt`). Bits 0-1 match the firmware.
FAULT_LOW_OIL = 1 << 0
FAULT_LOW_FUEL = 1 << 1
FAULT_OVER_TEMP = 1 << 2
FAULT_LOW_BATTERY = 1 << 3

# (ok, reason, fault_bits) - the pre-start / reset safety check
SafetyResult = tuple[bool, str, int]
Reply = tuple[int, dict]


class Generator:
    def __init__(self, safety_check: Callable[[], SafetyResult],
                 on_change: Callable[[RunState, RunState], None] | None = None,
                 crank_seconds: float = 2.5, stop_seconds: float = 3.0,
                 timer_factory: Callable[..., threading.Timer] = threading.Timer):
        self._safety_check = safety_check
        self._on_change = on_change
        self.crank_seconds = crank_seconds
        self.stop_seconds = stop_seconds
        self._timer_factory = timer_factory
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self.state = RunState.OFF
        self.flt = 0

    # ---- helpers -------------------------------------------------------------
    @property
    def state_name(self) -> str:
        return STATE_NAMES[self.state]

    def _set_state(self, new: RunState) -> None:
        """Change state under the lock; notify outside it (telemetry may block on I/O)."""
        with self._lock:
            old, self.state = self.state, new
        if old != new:
            log.info("generator %s -> %s", STATE_NAMES[old], STATE_NAMES[new])
            if self._on_change:
                self._on_change(old, new)

    def _schedule(self, seconds: float, fn: Callable[[], None]) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = self._timer_factory(seconds, fn)
            self._timer.daemon = True
            self._timer.start()

    def _cancel_timer(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None

    def _reply(self, status: int, result: str, **extra) -> Reply:
        body = {"result": result, "state": self.state_name}
        body.update(extra)
        return status, body

    # ---- commands ------------------------------------------------------------
    def start(self) -> Reply:
        with self._lock:
            if self.state in (RunState.CRANK, RunState.RUN):
                return self._reply(400, "refused", reason="Generator is already running")
            if self.state == RunState.STOP:
                return self._reply(400, "refused", reason="Generator is stopping; retry when OFF")
            if self.state == RunState.FAULT:
                return self._reply(409, "refused", reason="Fault lockout active; send reset_fault")
            ok, reason, bits = self._safety_check()
            if not ok:
                self.flt |= bits
        if not ok:
            self._set_state(RunState.FAULT)
            return self._reply(409, "refused", reason=reason)
        self._set_state(RunState.CRANK)
        self._schedule(self.crank_seconds, self._crank_done)
        return self._reply(200, "accepted")

    def _crank_done(self) -> None:
        with self._lock:
            if self.state != RunState.CRANK:
                return  # stopped or faulted while cranking
        self._set_state(RunState.RUN)

    def stop(self) -> Reply:
        with self._lock:
            if self.state in (RunState.OFF, RunState.STOP):
                return self._reply(200, "accepted", note="already off" if self.state == RunState.OFF
                                   else "already stopping")
            if self.state == RunState.FAULT:
                return self._reply(200, "accepted", note="engine not running (fault lockout)")
        self._cancel_timer()
        self._set_state(RunState.STOP)
        self._schedule(self.stop_seconds, self._stop_done)
        return self._reply(200, "accepted")

    def _stop_done(self) -> None:
        with self._lock:
            if self.state != RunState.STOP:
                return
        self._set_state(RunState.OFF)

    def reset_fault(self) -> Reply:
        with self._lock:
            if self.state != RunState.FAULT:
                return self._reply(200, "no_fault", note="nothing to reset")
            ok, reason, bits = self._safety_check()
            if not ok:
                self.flt = bits
        if not ok:
            return self._reply(409, "refused", reason=f"Condition still present: {reason}")
        with self._lock:
            self.flt = 0
        self._set_state(RunState.OFF)
        return self._reply(200, "cleared")

    def trip(self, bits: int, reason: str) -> bool:
        """A fault while running or cranking (M3 automatic faults): kill the engine.
        Returns False (and does nothing) if the engine is not cranking or running."""
        with self._lock:
            if self.state not in (RunState.CRANK, RunState.RUN):
                return False
        self._cancel_timer()
        with self._lock:
            self.flt |= bits
        log.warning("generator fault: %s", reason)
        self._set_state(RunState.FAULT)
        return True

    def shutdown(self) -> None:
        self._cancel_timer()
