"""One simulated EMS-1000 device: heartbeat loop around sensors -> payload -> transport."""

from __future__ import annotations

import logging
import threading

from .budget import BudgetGuard
from .config import SimConfig
from .persist import next_boot_count
from .sensors import SensorModel
from .telemetry import PayloadError, build_payload, new_traceparent, validate
from .transport import Transport

log = logging.getLogger(__name__)


class VirtualDevice:
    def __init__(self, device_id: str, cfg: SimConfig, transport: Transport,
                 budget: BudgetGuard | None, stop_event: threading.Event,
                 interval: int | None = None):
        self.device_id = device_id
        self.cfg = cfg
        self.transport = transport
        self.budget = budget          # None in dry-run mode
        self.stop_event = stop_event
        self.interval = interval if interval is not None else cfg.heartbeat_seconds
        self.sensors = SensorModel(cfg.sensors)
        self.boot = next_boot_count(cfg.state_dir, device_id)
        self.seq = 0
        self.flt = 0                  # fault bitmask (M3)
        self.run_state = 0            # 0=OFF (state machine arrives in M2)

    def send_telemetry(self, reason: str = "heartbeat") -> bool:
        if self.budget is not None and not self.budget.try_consume():
            return False
        self.seq += 1
        payload = build_payload(
            schema_version=self.cfg.schema_version, device_id=self.device_id,
            boot=self.boot, seq=self.seq, reading=self.sensors.read(),
            flt=self.flt, run=self.run_state,
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
        log.info("[%s] boot #%d, heartbeat every %ss", self.device_id, self.boot, self.interval)
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
            self.transport.disconnect()
            log.info("[%s] stopped after %d messages this run", self.device_id, self.seq)
