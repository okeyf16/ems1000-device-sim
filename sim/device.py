"""One simulated EMS-1000 device.

Heartbeat loop (sensors -> payload -> transport) plus, from M2, the generator state
machine and direct-method commands. Every state change sends an extra telemetry
message straight away, like the firmware's on-change publish.
"""

from __future__ import annotations

import dataclasses
import logging
import threading

from .budget import BudgetGuard
from .commands import CommandHandler
from .config import SimConfig
from .persist import next_boot_count
from .sensors import SensorModel
from .state_machine import STATE_NAMES, Generator, RunState
from .telemetry import PayloadError, build_payload, new_traceparent, validate
from .transport import Transport

log = logging.getLogger(__name__)


class VirtualDevice:
    def __init__(self, device_id: str, cfg: SimConfig, transport: Transport,
                 budget: BudgetGuard | None, stop_event: threading.Event,
                 interval: int | None = None, timer_factory=threading.Timer):
        self.device_id = device_id
        self.cfg = cfg
        self.transport = transport
        self.budget = budget          # None in dry-run mode
        self.stop_event = stop_event
        self.interval = interval if interval is not None else cfg.heartbeat_seconds
        self.sensors = SensorModel(cfg.sensors)
        self.boot = next_boot_count(cfg.state_dir, device_id)
        self.seq = 0
        self._send_lock = threading.Lock()  # heartbeat, timers and commands run on different threads

        self.generator = Generator(
            safety_check=self.sensors.safety_check,
            on_change=self._on_state_change,
            crank_seconds=cfg.crank_seconds,
            stop_seconds=cfg.stop_seconds,
            timer_factory=timer_factory,
        )
        self.commands = CommandHandler(
            self.generator,
            snapshot=lambda: dataclasses.asdict(self.sensors.read(int(self.generator.state))),
            on_command=(lambda: self.budget.record()) if self.budget else None,
        )
        self.transport.set_method_handler(self.commands.handle)

    # Kept for compatibility with M1 callers/tests
    @property
    def run_state(self) -> int:
        return int(self.generator.state)

    @property
    def flt(self) -> int:
        return self.generator.flt

    def _on_state_change(self, old: RunState, new: RunState) -> None:
        self.send_telemetry(reason=f"state {STATE_NAMES[old]}->{STATE_NAMES[new]}")

    def send_telemetry(self, reason: str = "heartbeat") -> bool:
        with self._send_lock:
            if self.budget is not None and not self.budget.try_consume():
                return False
            self.seq += 1
            run = int(self.generator.state)
            payload = build_payload(
                schema_version=self.cfg.schema_version, device_id=self.device_id,
                boot=self.boot, seq=self.seq, reading=self.sensors.read(run),
                flt=self.generator.flt, run=run,
            )
            try:
                body = validate(payload, self.cfg.max_payload_bytes)
            except PayloadError as err:
                log.error("[%s] payload rejected, not sent: %s", self.device_id, err)
                return False
            self.transport.send_telemetry(body, new_traceparent())
            used = f" | budget {self.budget.used}/{self.cfg.daily_limit}" if self.budget else ""
            log.info("[%s] %s #%d sent (%dB)%s\n    %s", self.device_id, reason,
                     self.seq, len(body), used, body)
            return True

    def run(self, max_messages: int | None = None) -> None:
        log.info("[%s] boot #%d, heartbeat every %ss, listening for commands",
                 self.device_id, self.boot, self.interval)
        self.transport.connect()
        try:
            sent = 0
            while not self.stop_event.is_set():
                if self.send_telemetry():
                    sent += 1
                if max_messages is not None and sent >= max_messages:
                    break
                self.stop_event.wait(self.interval)
        finally:
            self.generator.shutdown()
            self.transport.disconnect()
            log.info("[%s] stopped after %d messages this run", self.device_id, self.seq)
