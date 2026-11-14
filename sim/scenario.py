"""YAML scenarios: a scripted sequence of actions and waits, played against one device.

Format (see config/scenarios/*.yaml):

  name: out-of-fuel
  description: what it shows
  speed: 60                  # used when --speed is not given
  setup:                     # overrides config `sensors:` start values
    fuel_start_pct: 20
  steps:
    - say: "text to log"
    - do: start              # any action from sim/controls.py, optional `value:`
    - until: {state: RUN}    # wait for a condition; optional `timeout:` (engine seconds)
    - wait: 600              # engine seconds (real seconds = engine seconds / speed)

Conditions for `until`: state (OFF, CRANKING, RUNNING, STOPPING, FAULT), temp_below,
temp_above, fuel_below (percent), breaker (tripped | ok). Several are combined with AND.
A timeout is never shorter than 30 real seconds, because cranking and stopping take
real time whatever the speed.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .controls import ACTION_NAMES, Controls

log = logging.getLogger(__name__)

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "config" / "scenarios"
STEP_KEYS = {"say", "do", "until", "wait"}
CONDITIONS = {"state", "temp_below", "temp_above", "fuel_below", "breaker"}
STATES = {"OFF", "CRANKING", "RUNNING", "STOPPING", "FAULT"}
DEFAULT_TIMEOUT_S = 24 * 3600   # engine seconds


def _state(value) -> str:
    """YAML reads a bare OFF as the boolean false: accept it as the OFF state."""
    return "OFF" if value is False else str(value).upper()


@dataclass
class Scenario:
    name: str
    description: str = ""
    speed: float | None = None
    setup: dict = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)


def available() -> list[str]:
    return sorted(p.stem for p in SCENARIO_DIR.glob("*.yaml"))


def load_scenario(name_or_path: str) -> Scenario:
    path = Path(name_or_path)
    if not path.suffix:
        path = SCENARIO_DIR / f"{name_or_path}.yaml"
    if not path.is_file():
        raise ValueError(f"scenario {name_or_path!r} not found; available: {', '.join(available())}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    steps = raw.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"{path.name}: 'steps' must be a non-empty list")
    actions = ACTION_NAMES
    for n, step in enumerate(steps, 1):
        where = f"{path.name} step {n}"
        if not isinstance(step, dict):
            raise ValueError(f"{where}: must be a mapping, got {step!r}")
        kinds = STEP_KEYS & step.keys()
        if len(kinds) != 1:
            raise ValueError(f"{where}: needs exactly one of {sorted(STEP_KEYS)}")
        extra = set(step) - kinds - {"value", "timeout"}
        if extra:
            raise ValueError(f"{where}: unknown keys {sorted(extra)}")
        if "do" in step and step["do"] not in actions:
            raise ValueError(f"{where}: unknown action {step['do']!r}; use one of {sorted(actions)}")
        if "until" in step:
            cond = step["until"]
            if not isinstance(cond, dict) or not cond or set(cond) - CONDITIONS:
                raise ValueError(f"{where}: 'until' needs conditions from {sorted(CONDITIONS)}")
            if "state" in cond and _state(cond["state"]) not in STATES:
                raise ValueError(f"{where}: state must be one of {sorted(STATES)}")
        if "wait" in step and float(step["wait"]) < 0:
            raise ValueError(f"{where}: wait must be >= 0")
    speed = raw.get("speed")
    return Scenario(name=str(raw.get("name") or path.stem), description=str(raw.get("description") or ""),
                    speed=float(speed) if speed is not None else None,
                    setup=dict(raw.get("setup") or {}), steps=steps)


class ScenarioRunner:
    def __init__(self, scenario: Scenario, controls: Controls, stop_event: threading.Event,
                 poll_s: float = 0.1, min_real_timeout_s: float = 30.0):
        self.scenario = scenario
        self.controls = controls
        self.device = controls.device
        self.stop_event = stop_event          # the whole sim is stopping
        self.cancel_event = threading.Event()  # just this scenario (panel Stop button)
        self.poll_s = poll_s
        self.min_real_timeout_s = min_real_timeout_s
        self.completed = False
        # progress, for the web panel
        self.step = 0
        self.step_text = ""
        self.result = "running"  # running | finished | stopped | timed out | failed

    def cancel(self) -> None:
        self.cancel_event.set()

    @property
    def running(self) -> bool:
        return self.result == "running"

    def progress(self) -> dict:
        return {"name": self.scenario.name, "description": self.scenario.description,
                "step": self.step, "total": len(self.scenario.steps), "text": self.step_text,
                "result": self.result, "running": self.running}

    def _stopped(self) -> bool:
        return self.stop_event.is_set() or self.cancel_event.is_set()

    def _wait(self, seconds: float) -> bool:
        """Wait up to `seconds` real seconds; True if the scenario should stop."""
        end = time.monotonic() + seconds
        while not self._stopped():
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            self.cancel_event.wait(min(remaining, 0.2))
        return True

    def _engine_to_real(self, seconds: float) -> float:
        return seconds / max(self.device.speed, 1e-9)

    def _met(self, cond: dict) -> bool:
        d, m = self.device, self.device.model
        checks = {
            "state": lambda v: d.generator.state_name == _state(v),
            "temp_below": lambda v: m.temp < float(v),
            "temp_above": lambda v: m.temp > float(v),
            "fuel_below": lambda v: m.fuel_pct < float(v),
            "breaker": lambda v: m.breaker_tripped == (str(v).lower() == "tripped"),
        }
        return all(checks[k](v) for k, v in cond.items())

    def _end(self, result: str) -> bool:
        self.result = result
        if result == "stopped":
            log.info("Scenario '%s' stopped at step %d", self.scenario.name, self.step)
        return result == "finished"

    def run(self) -> bool:
        sc = self.scenario
        log.info("Scenario '%s' started (%d steps, speed %gx)%s", sc.name, len(sc.steps),
                 self.device.speed, f": {sc.description}" if sc.description else "")
        for n, step in enumerate(sc.steps, 1):
            if self._stopped():
                return self._end("stopped")
            self.step = n
            tag = f"[scenario {n}/{len(sc.steps)}]"
            if "say" in step:
                self.step_text = str(step["say"])
                log.info("%s %s", tag, step["say"])
            elif "do" in step:
                self.step_text = f"{step['do']}" + (f" {step['value']}" if "value" in step else "")
                result = self.controls.do(step["do"], step.get("value"))
                self.step_text += f": {result}"
                log.info("%s %s", tag, self.step_text)
            elif "wait" in step:
                secs = float(step["wait"])
                self.step_text = f"wait {_fmt(secs)} engine time"
                log.info("%s %s", tag, self.step_text)
                if self._wait(self._engine_to_real(secs)):
                    return self._end("stopped")
            elif "until" in step:
                cond = step["until"]
                timeout = float(step.get("timeout", DEFAULT_TIMEOUT_S))
                what = ", ".join(f"{k}={_state(v) if k == 'state' else v}" for k, v in cond.items())
                self.step_text = f"wait until {what}"
                log.info("%s %s (timeout %s engine time)", tag, self.step_text, _fmt(timeout))
                deadline = time.monotonic() + max(self._engine_to_real(timeout), self.min_real_timeout_s)
                while not self._met(cond):
                    if time.monotonic() > deadline:
                        log.warning("%s timed out waiting for %s; scenario stopped", tag, cond)
                        return self._end("timed out")
                    if self._wait(self.poll_s):
                        return self._end("stopped")
        self.completed = True
        self.step_text = "finished"
        log.info("Scenario '%s' finished. The sim keeps running (Ctrl+C to stop).", sc.name)
        return self._end("finished")


def _fmt(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:g} h"
    if seconds >= 60:
        return f"{seconds / 60:g} min"
    return f"{seconds:g} s"


def start_scenario(runner: ScenarioRunner) -> threading.Thread:
    def go():
        try:
            runner.run()
        except Exception:  # noqa: BLE001
            runner.result = "failed"
            log.exception("scenario failed")
    t = threading.Thread(target=go, name="scenario", daemon=True)
    t.start()
    return t
