"""One simulated EMS-1000 device.

Heartbeat loop (model -> payload -> transport), the generator state machine and
direct-method commands (M2), and the physics-lite ECO4990ES model (M3), stepped
once per tick. Every state change, new alert or warning sends an extra telemetry
message straight away, like the firmware's on-change publish.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time

from .budget import BudgetGuard
from .commands import CommandHandler
from .config import SimConfig
from .persist import next_boot_count
from .physics import GeneratorModel
from .state_machine import STATE_NAMES, Generator, RunState
from .telemetry import PayloadError, build_payload, new_traceparent, validate
from .transport import QUOTA_MESSAGE, Transport, is_quota_error
from .twin import TwinManager

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
        self.model = GeneratorModel(cfg.sensors)
        self.speed = cfg.speed        # model seconds per real second (--speed)
        self.boot = next_boot_count(cfg.state_dir, device_id)
        self.seq = 0
        self._send_lock = threading.Lock()  # heartbeat, timers and commands run on different threads

        self.generator = Generator(
            safety_check=self.model.safety_check,
            on_change=self._on_state_change,
            crank_seconds=cfg.crank_seconds,
            stop_seconds=cfg.stop_seconds,
            timer_factory=timer_factory,
        )
        self.commands = CommandHandler(
            self.generator,
            snapshot=self._snapshot,
            on_command=(lambda: self.budget.record()) if self.budget else None,
        )
        self.transport.set_method_handler(self.commands.handle)
        self._wake = threading.Event()
        self.twin = TwinManager(self)
        self.transport.set_desired_handler(self.twin.on_desired_patch)
        self.fatal_reason: str | None = None
        self.transport.on_fatal = self.fatal

    def fatal(self, reason: str) -> None:
        """Stop the whole run (all simulated devices) for a reason that retrying would make
        worse: a reconnect storm or the hub's daily quota."""
        if self.fatal_reason is None:
            self.fatal_reason = reason
            log.error("[%s] stopping: %s", self.device_id, reason)
        self.stop_event.set()

    def wake(self) -> None:
        """Re-check the heartbeat timer now (e.g. the interval was changed remotely)."""
        self._wake.set()

    # Kept for compatibility with M1/M2 callers and tests
    @property
    def sensors(self) -> GeneratorModel:
        return self.model

    @property
    def run_state(self) -> int:
        return int(self.generator.state)

    @property
    def flt(self) -> int:
        """Telemetry bitmask: latched faults (0-3) plus live alerts (4-5)."""
        return self.generator.flt | self.model.alert_bits

    def _snapshot(self) -> dict:
        snap = dataclasses.asdict(self.model.read(int(self.generator.state)))
        snap["flt"] = self.flt
        snap["model"] = self.model.extras()
        return snap

    def tick(self, dt_model: float) -> None:
        """Advance the model by dt_model simulated seconds and act on what happened."""
        ev = self.model.step(dt_model, int(self.generator.state))
        tripped = False
        for bits, reason in ev.faults:
            tripped = self.generator.trip(bits, reason) or tripped  # sends telemetry on change
        if not tripped and (ev.alerts_changed or ev.warnings_changed or ev.breaker_tripped):
            what = ", ".join(w for w, on in (("breaker tripped", ev.breaker_tripped),
                                             ("alerts", ev.alerts_changed),
                                             ("warnings", ev.warnings_changed)) if on)
            self.send_telemetry(reason=f"change: {what} {sorted(self.model.warnings)}")

    def _ticker(self) -> None:
        step = self.cfg.tick_seconds
        while not self.stop_event.wait(step):
            try:
                self.tick(step * self.speed)
            except Exception:  # noqa: BLE001 - keep the device alive
                log.exception("[%s] model tick failed", self.device_id)

    def _on_state_change(self, old: RunState, new: RunState) -> None:
        self.send_telemetry(reason=f"state {STATE_NAMES[old]}->{STATE_NAMES[new]}")
        self.twin.report_state()

    def send_telemetry(self, reason: str = "heartbeat") -> bool:
        with self._send_lock:
            if self.budget is not None and not self.budget.try_consume():
                return False
            self.seq += 1
            run = int(self.generator.state)
            payload = build_payload(
                schema_version=self.cfg.schema_version, device_id=self.device_id,
                boot=self.boot, seq=self.seq, reading=self.model.read(run),
                flt=self.flt, run=run,
            )
            try:
                body = validate(payload, self.cfg.max_payload_bytes)
            except PayloadError as err:
                log.error("[%s] payload rejected, not sent: %s", self.device_id, err)
                return False
            try:
                self.transport.send_telemetry(body, new_traceparent())
            except Exception as err:  # noqa: BLE001 - a dropped connection must not end the run
                if self.budget is not None:
                    self.budget.record(-1)          # reserved but never sent
                if is_quota_error(err):
                    self.fatal(QUOTA_MESSAGE)
                else:
                    log.warning("[%s] %s #%d not sent: not connected to IoT Hub (%s); the SDK is "
                                "reconnecting", self.device_id, reason, self.seq, type(err).__name__)
                return False
            used = f" | budget {self.budget.used}/{self.cfg.daily_limit}" if self.budget else ""
            log.info("[%s] %s #%d sent (%dB)%s\n    %s", self.device_id, reason,
                     self.seq, len(body), used, body)
            return True

    def _sleep_until_next_beat(self, last_beat: float) -> None:
        """Wait `interval` seconds, re-reading it so a remote change applies straight away."""
        while not self.stop_event.is_set():
            remaining = last_beat + self.interval - time.monotonic()
            if remaining <= 0:
                return
            self._wake.wait(min(remaining, 0.5))
            self._wake.clear()

    def run(self, max_messages: int | None = None) -> None:
        log.info("[%s] boot #%d, heartbeat every %ss, model speed %gx, listening for commands",
                 self.device_id, self.boot, self.interval, self.speed)
        try:
            self.transport.connect()
        except Exception as err:  # noqa: BLE001
            self.fatal(QUOTA_MESSAGE if is_quota_error(err) else
                       f"could not connect to IoT Hub: {err} (check the connection string and the hub)")
            self.transport.disconnect()
            return
        self.twin.start()
        threading.Thread(target=self._ticker, name=f"{self.device_id}-model", daemon=True).start()
        try:
            sent = 0
            while not self.stop_event.is_set():
                if self.send_telemetry():
                    sent += 1
                if max_messages is not None and sent >= max_messages:
                    break
                self._sleep_until_next_beat(time.monotonic())
        finally:
            self.generator.shutdown()
            self.transport.disconnect()
            log.info("[%s] stopped after %d messages this run", self.device_id, self.seq)
