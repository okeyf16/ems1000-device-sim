"""Device twin (M5): reported properties and remotely changeable settings.

Reported (on start-up, state/fault changes and when settings are applied, never
periodically): device info, state, last fault, the settings in use and a status for
each desired setting.

Desired settings are validated here. A bad value is rejected and reported with a
reason; the other settings in the same patch still apply. A setting set to null goes
back to the config value. Desired settings persist in IoT Hub, so they also override
config/default.yaml at start-up.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import __version__
from .budget import blocks
from .state_machine import STATE_NAMES
from .transport import QUOTA_MESSAGE, is_quota_error

log = logging.getLogger(__name__)

MODEL_NAME = "Sumec Firman ECO4990ES"


@dataclass(frozen=True)
class Setting:
    name: str
    low: float
    high: float
    integer: bool
    get: Callable[[Any], float]          # device -> current value
    set: Callable[[Any, float], None]    # (device, value) -> None


def _set_interval(d, v):
    d.interval = int(v)
    d.wake()                      # apply from the next heartbeat, not after the old wait


SETTINGS: dict[str, Setting] = {s.name: s for s in (
    Setting("telemetry_interval_s", 10, 3600, True, lambda d: d.interval, _set_interval),
    Setting("load_pct", 0, 150, False, lambda d: round(d.model.load * 100, 1),
            lambda d, v: d.model.set_load_pct(v)),
    Setting("low_fuel_warn_pct", 1, 50, False, lambda d: d.model.low_fuel_warn_pct,
            lambda d, v: setattr(d.model, "low_fuel_warn_pct", float(v))),
    Setting("overtemp_c", 85, 120, False, lambda d: d.model.overtemp_c,
            lambda d, v: setattr(d.model, "overtemp_c", float(v))),
    Setting("min_start_battery_v", 10.0, 12.5, False, lambda d: d.model.min_start_v,
            lambda d, v: setattr(d.model, "min_start_v", float(v))),
    Setting("speed", 1, 3600, False, lambda d: d.speed, lambda d, v: setattr(d, "speed", float(v))),
)}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(setting: Setting, value: Any) -> tuple[float | None, str]:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None, f"must be a number, got {value!r}"
    if setting.integer and float(value) != int(value):
        return None, "must be a whole number"
    if not setting.low <= value <= setting.high:
        return None, f"must be between {setting.low:g} and {setting.high:g}"
    return (int(value) if setting.integer else float(value)), ""


class TwinManager:
    def __init__(self, device):
        self.device = device
        self.transport = device.transport
        self._lock = threading.Lock()
        self.defaults = {name: s.get(device) for name, s in SETTINGS.items()}  # config values
        self.status: dict[str, dict] = {}
        self.enabled = False

    # ---- reporting -------------------------------------------------------------------
    def settings(self) -> dict:
        return {name: s.get(self.device) for name, s in SETTINGS.items()}

    def _report(self, patch: dict) -> None:
        if not self.enabled:
            return
        budget = self.device.budget
        cost = blocks(patch)
        if budget is not None and not budget.try_consume(cost):
            log.warning("[%s] twin update skipped (daily budget reached)", self.device.device_id)
            return
        try:
            self.transport.patch_reported(patch)
        except Exception as err:  # noqa: BLE001 - twin trouble must not stop the device
            if budget is not None:
                budget.record(-cost)
            if is_quota_error(err):
                self.device.fatal(QUOTA_MESSAGE)
                return
            log.exception("[%s] twin reported-properties update failed", self.device.device_id)

    def state_patch(self) -> dict:
        g = self.device.generator
        return {"state": STATE_NAMES[g.state], "run": int(g.state), "flt": self.device.flt}

    def report_state(self) -> None:
        patch = self.state_patch()
        g = self.device.generator
        if STATE_NAMES[g.state] == "FAULT":
            patch["last_fault"] = {"bits": g.flt, "reason": g.fault_reason, "at": _now()}
        self._report(patch)

    # ---- desired settings -------------------------------------------------------------
    def apply_desired(self, desired: dict, startup: bool = False) -> dict:
        """Apply a desired patch (or the full desired document at start-up)."""
        version = desired.get("$version")
        status: dict[str, Any] = {}
        with self._lock:
            for key, value in desired.items():
                if key.startswith("$"):
                    continue
                setting = SETTINGS.get(key)
                if setting is None:
                    status[key] = {"status": "rejected", "reason": f"unknown setting; use one of "
                                   f"{sorted(SETTINGS)}", "desired_version": version}
                    continue
                if value is None:
                    setting.set(self.device, self.defaults[key])
                    status[key] = None                       # remove the status entry
                    log.info("[%s] twin: %s cleared, back to config value %s",
                             self.device.device_id, key, self.defaults[key])
                    continue
                clean, reason = _validate(setting, value)
                if clean is None:
                    status[key] = {"status": "rejected", "reason": reason, "desired_version": version}
                    log.warning("[%s] twin: %s=%r rejected: %s", self.device.device_id, key, value, reason)
                    continue
                setting.set(self.device, clean)
                status[key] = {"status": "applied", "desired_version": version}
                log.info("[%s] twin: %s = %s%s", self.device.device_id, key, clean,
                         " (overrides config)" if startup and clean != self.defaults[key] else "")
            for key, st in status.items():
                if st is None:
                    self.status.pop(key, None)
                else:
                    self.status[key] = st
        return status

    def on_desired_patch(self, patch: dict) -> None:
        status = self.apply_desired(patch)
        if status:
            self._report({"settings": self.settings(), "settings_status": status})

    # ---- lifecycle -------------------------------------------------------------------
    def start(self) -> None:
        """After connect: read the twin, apply stored desired settings, report everything."""
        try:
            twin = self.transport.get_twin() or {}
        except Exception as err:  # noqa: BLE001
            if is_quota_error(err):
                self.device.fatal(QUOTA_MESSAGE)
                return
            log.exception("[%s] could not read the device twin; running without it", self.device.device_id)
            return
        if self.device.budget is not None:
            self.device.budget.record(blocks(twin))   # twin reads count too
        self.enabled = True
        desired = twin.get("desired") or {}
        old_status = (twin.get("reported") or {}).get("settings_status") or {}
        self.apply_desired(desired, startup=True)
        # replace any stale status entries; remove the whole block when nothing is desired
        status_patch: dict[str, Any] | None = None
        if self.status:
            status_patch = {k: None for k in old_status if k not in self.status}
            status_patch.update(self.status)
        g = self.device.generator
        self._report({
            "device": {"model": MODEL_NAME, "sim_version": __version__, "boot": self.device.boot,
                       "started_at": _now()},
            **self.state_patch(),
            "settings": self.settings(),
            "settings_status": status_patch,
            **({"last_fault": {"bits": g.flt, "reason": g.fault_reason, "at": _now()}}
               if STATE_NAMES[g.state] == "FAULT" else {}),
        })
