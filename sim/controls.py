"""Local controls for one simulated device, shared by the keyboard shortcuts and scenarios.

These act on the simulator directly (like someone at the generator), so they use no
IoT Hub messages. start/stop/reset_fault go through the same state machine and
interlock as the remote commands.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

OIL_NAMES = {0: "OK", 1: "low (warning)", 2: "critical"}
ACTION_NAMES = frozenset({
    "start", "stop", "reset_fault", "load", "load_up", "load_down", "oil", "oil_cycle",
    "fuel", "fuel_low", "fuel_empty", "refuel", "overheat", "weak_battery",
    "reset_breaker", "send", "status", "speed",
})


def _reply_text(reply: tuple[int, dict]) -> str:
    status, body = reply
    extra = body.get("reason") or body.get("note") or ""
    return f"{status} {body.get('result')}" + (f" - {extra}" if extra else "")


class Controls:
    """Named actions on a VirtualDevice. Each returns a short description of the result."""

    def __init__(self, device):
        self.device = device
        self.model = device.model
        self.actions: dict[str, Callable[[Any], str]] = {
            "start": lambda _=None: _reply_text(device.generator.start()),
            "stop": lambda _=None: _reply_text(device.generator.stop()),
            "reset_fault": lambda _=None: _reply_text(device.generator.reset_fault()),
            "load": self.load,
            "load_up": lambda _=None: self.load(self._load_pct() + 10),
            "load_down": lambda _=None: self.load(self._load_pct() - 10),
            "oil": self.oil,
            "oil_cycle": lambda _=None: self.oil((self.model.oil + 1) % 3),
            "fuel": self.fuel,
            "fuel_low": lambda _=None: self.fuel(9),
            "fuel_empty": lambda _=None: self.fuel(0),
            "refuel": lambda _=None: self.fuel(100),
            "overheat": self.overheat,
            "weak_battery": self.weak_battery,
            "reset_breaker": self.reset_breaker,
            "send": self.send,
            "status": self.status,
            "speed": self.speed,
        }
        assert set(self.actions) == ACTION_NAMES

    def do(self, action: str, value: Any = None) -> str:
        if action not in self.actions:
            raise ValueError(f"unknown action {action!r}; use one of {sorted(self.actions)}")
        return self.actions[action](value)

    # ---- actions ---------------------------------------------------------------------
    def _load_pct(self) -> float:
        return round(self.model.load * 100)

    def load(self, pct: Any) -> str:
        self.model.set_load_pct(max(0.0, min(150.0, float(pct))))
        pct = self._load_pct()
        note = ""
        if self.model.load > self.model.max_load:
            note = f" - above max {self.model.max_load * 100:.0f} %: breaker trips after " \
                   f"{self.model.overload_trip_s:.0f} s"
        elif self.model.load > 1:
            note = " - above rated: voltage/frequency alerts"
        return f"load {pct:.0f} % (~{self.model.load * self.model.rated_a:.1f} A){note}"

    def oil(self, level: Any) -> str:
        self.model.set_oil(int(level))
        return f"oil {self.model.oil} ({OIL_NAMES[self.model.oil]})"

    def fuel(self, pct: Any) -> str:
        self.model.set_fuel_pct(float(pct))
        return f"fuel {self.model.fuel_pct:.0f} % ({self.model.fuel_l:.1f} L)"

    @staticmethod
    def _flag(value: Any, current: bool) -> bool:
        if value is None:
            return not current  # toggle
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "on", "yes")
        return bool(value)

    def overheat(self, value: Any = None) -> str:
        self.model.set_overheat(self._flag(value, self.model.overheat))
        return f"overheat {'ON (cooling blocked)' if self.model.overheat else 'off'}"

    def weak_battery(self, value: Any = None) -> str:
        self.model.set_weak_battery(self._flag(value, self.model.weak_battery))
        return f"weak battery {'ON' if self.model.weak_battery else 'off'}"

    def reset_breaker(self, _: Any = None) -> str:
        ok, msg = self.model.reset_breaker()
        if ok and msg == "breaker reset":
            self.device.send_telemetry(reason="breaker reset")
        return msg if ok else f"refused - {msg}"

    def speed(self, value: Any) -> str:
        self.device.speed = max(1.0, min(3600.0, float(value)))
        return f"speed {self.device.speed:g}x"

    def send(self, _: Any = None) -> str:
        return "telemetry sent" if self.device.send_telemetry(reason="manual") else "not sent (budget)"

    def status(self, _: Any = None) -> str:
        d = self.device
        snap = d._snapshot()
        extras = snap.pop("model")
        return (f"{d.generator.state_name} | " + json.dumps(snap, separators=(",", ":")) +
                f" | load {extras['load_pct']} %, {extras['fuel_l_per_h']} L/h, "
                f"breaker {'TRIPPED' if extras['breaker_tripped'] else 'ok'}, "
                f"warnings {extras['warnings'] or 'none'}, speed {d.speed:g}x")


# key -> (action, label for the help text)
HOTKEYS: dict[str, tuple[str, str]] = {
    "s": ("start", "start"),
    "x": ("stop", "stop"),
    "r": ("reset_fault", "reset fault"),
    "+": ("load_up", "load +10 %"),
    "-": ("load_down", "load -10 %"),
    "o": ("oil_cycle", "oil OK -> low -> critical"),
    "f": ("fuel_low", "fuel 9 % (low-fuel warning)"),
    "e": ("fuel_empty", "fuel empty"),
    "F": ("refuel", "refuel to 100 %"),
    "h": ("overheat", "overheat on/off"),
    "b": ("weak_battery", "weak battery on/off"),
    "p": ("reset_breaker", "reset breaker"),
    "t": ("send", "send telemetry now"),
    "i": ("status", "show status"),
}
ALT_KEYS = {"=": "+", "_": "-"}  # unshifted + and shifted - on most keyboards


def help_text() -> str:
    rows = [f"  {k}  {label}" for k, (_, label) in HOTKEYS.items()]
    return "Keyboard shortcuts:\n" + "\n".join(rows) + "\n  ?  this list    Ctrl+C  quit"


def handle_key(controls: Controls, key: str) -> str | None:
    """Run the action for one key press; None for keys with no action."""
    key = ALT_KEYS.get(key, key)
    if key == "?":
        return help_text()
    if key not in HOTKEYS:
        return None
    action, label = HOTKEYS[key]
    try:
        return f"[{key}] {label}: {controls.do(action)}"
    except Exception as err:  # noqa: BLE001 - a bad key press must not kill the sim
        log.exception("key %r failed", key)
        return f"[{key}] {label}: error {err}"

