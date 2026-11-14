"""Physics-lite model of a Sumec Firman ECO4990ES (M3).

Calibrated to the retailer specs: 3.0 kVA rated / 3.2 kVA max, 220 V / 50 Hz single
phase, conventional brush alternator with AVR (not an inverter), SPE200E engine,
15 L tank lasting ~12 h at 50 % load, overload and low-oil protection, electric start.

The model advances in steps of simulated time (`step(dt, run_state)`) and returns the
events the device must act on (automatic faults, alert/warning changes). It never calls
the state machine itself, so no lock is held while telemetry is sent.

Formulas marked "estimate" are calibrated to one spec figure, not measured.
"""

from __future__ import annotations

import math
import random
import threading
from dataclasses import dataclass, field

from .sensors import SensorReading

# Telemetry `run` codes (kept local to avoid an import cycle with state_machine)
OFF, CRANK, RUN, STOP, FAULT = 0, 1, 2, 3, 4

# Fault bits (latched by the state machine) and alert bits (live, from the model)
FAULT_LOW_OIL = 1 << 0
FAULT_LOW_FUEL = 1 << 1
FAULT_OVER_TEMP = 1 << 2
FAULT_LOW_BATTERY = 1 << 3
ALERT_VOLTAGE = 1 << 4
ALERT_FREQUENCY = 1 << 5


@dataclass
class StepEvents:
    faults: list[tuple[int, str]] = field(default_factory=list)   # (bits, reason) -> trip the engine
    alerts_changed: bool = False
    warnings_changed: bool = False
    breaker_tripped: bool = False


def _approach(value: float, target: float, dt: float, tau: float) -> float:
    """First-order lag towards target with time constant tau (seconds)."""
    if tau <= 0:
        return target
    return target + (value - target) * math.exp(-dt / tau)


class GeneratorModel:
    def __init__(self, cfg: dict, rng: random.Random | None = None):
        g = lambda k, d: float(cfg.get(k, d))  # noqa: E731
        self.rng = rng or random.Random()
        self.noise = bool(cfg.get("noise", True))
        self._lock = threading.RLock()

        # Nameplate (ECO4990ES)
        self.rated_kva = g("rated_kva", 3.0)
        self.max_kva = g("max_kva", 3.2)
        self.nominal_v = g("nominal_v", 220.0)
        self.tank_l = g("tank_l", 15.0)
        self.rated_a = self.rated_kva * 1000 / self.nominal_v          # ~13.6 A
        self.max_load = self.max_kva / self.rated_kva                    # ~1.067 of rated

        # Fuel (estimate): L/h = base + per_load * load_fraction -> 1.25 L/h at 50 % = 12 h/15 L
        self.fuel_base_lph = g("fuel_l_per_h_base", 0.55)
        self.fuel_load_lph = g("fuel_l_per_h_per_load", 1.40)

        # Thermal (estimate)
        self.ambient = g("ambient_temp_c", 28.0)
        self.heat_tau = g("heat_tau_s", 480.0)
        self.cool_tau = g("cool_tau_s", 900.0)
        self.overtemp_c = g("overtemp_c", 95.0)
        self.restart_temp_c = g("restart_temp_c", 80.0)

        # Battery
        self.battery_rest = g("battery_rest_v", 12.6)
        self.charging_v = g("charging_v", 13.8)
        self.crank_drop = g("crank_drop_v", 2.0)
        self.min_start_v = g("min_start_battery_v", 11.0)
        self.weak_battery_v = g("weak_battery_v", 10.8)

        # Thresholds
        self.low_fuel_warn_pct = g("low_fuel_warn_pct", 10.0)
        self.low_fuel_interlock_pct = g("low_fuel_interlock_pct", 5.0)
        self.overload_trip_s = g("overload_trip_s", 5.0)
        self.v_tolerance = g("voltage_tolerance_pct", 5.0) / 100
        self.f_low, self.f_high = g("frequency_low_hz", 49.5), g("frequency_high_hz", 52.0)

        # State
        self.fuel_l = self.tank_l * g("fuel_start_pct", 85.0) / 100
        self.oil = int(cfg.get("oil_start", 0))
        self.load = g("load_pct", 25.0) / 100
        self.temp = self.ambient
        self.battery_v = self.battery_rest
        self.rssi = int(cfg.get("rssi_start_dbm", -68))
        self.overheat = False          # injected cooling failure
        self.weak_battery = False      # injected weak starter battery
        self.breaker_tripped = False
        self._overload_s = 0.0
        self.alert_bits = 0
        self._warnings: frozenset[str] = frozenset()

    # ---- derived values -------------------------------------------------------------
    @property
    def fuel_pct(self) -> float:
        return max(0.0, min(100.0, self.fuel_l / self.tank_l * 100))

    def fuel_lph(self, load: float | None = None) -> float:
        return self.fuel_base_lph + self.fuel_load_lph * (self.load if load is None else load)

    def _output(self, run_state: int) -> tuple[float, float, float]:
        """(volts, amps, hertz) at the outlets."""
        if run_state != RUN:
            return 0.0, 0.0, 0.0
        lf = self.load
        # Governor droop: ~51.5 Hz no load -> 50 Hz at rated; dips harder in overload
        f = 51.5 - 1.5 * min(lf, 1.0) - max(0.0, lf - 1.0) * 10.0
        if self.breaker_tripped:
            return 0.0, 0.0, f
        # AVR holds voltage within a few volts; sags hard past rated
        v = self.nominal_v - 6.0 * min(lf, 1.0) - max(0.0, lf - 1.0) * 60.0
        return v, lf * self.rated_a, f

    @property
    def warnings(self) -> frozenset[str]:
        return self._warnings

    # ---- simulation step ------------------------------------------------------------
    def step(self, dt: float, run_state: int) -> StepEvents:
        ev = StepEvents()
        with self._lock:
            running = run_state == RUN
            # Fuel
            if running:
                self.fuel_l = max(0.0, self.fuel_l - self.fuel_lph() * dt / 3600)
            # Temperature
            if running:
                target = self.ambient + 35 + 25 * min(self.load, 1.2) + (60 if self.overheat else 0)
                self.temp = _approach(self.temp, target, dt, self.heat_tau)
            else:
                self.temp = _approach(self.temp, self.ambient, dt, self.cool_tau)
            # Battery
            rest = self.weak_battery_v if self.weak_battery else self.battery_rest
            if running:
                self.battery_v = _approach(self.battery_v, self.charging_v, dt, 300)
            elif run_state == CRANK:
                self.battery_v = rest - self.crank_drop
            else:
                self.battery_v = _approach(self.battery_v, rest, dt, 120)
            # Overload protection: trips after overload_trip_s above max kVA
            if running and not self.breaker_tripped and self.load > self.max_load:
                self._overload_s += dt
                if self._overload_s >= self.overload_trip_s:
                    self.breaker_tripped = True
                    ev.breaker_tripped = True
            else:
                self._overload_s = 0.0

            # Automatic faults (engine running or cranking)
            if run_state in (CRANK, RUN):
                if self.oil >= 2:
                    ev.faults.append((FAULT_LOW_OIL, "Low oil protection stopped the engine"))
                if running and self.fuel_l <= 0:
                    ev.faults.append((FAULT_LOW_FUEL, "Out of fuel: engine stalled"))
                if running and self.temp > self.overtemp_c:
                    ev.faults.append((FAULT_OVER_TEMP, f"Engine over-temperature ({self.temp:.0f} C)"))

            # Alerts (live bits): output out of tolerance, or breaker tripped
            bits = 0
            if running:
                v, _, f = self._output(run_state)
                if self.breaker_tripped:
                    bits = ALERT_VOLTAGE | ALERT_FREQUENCY
                else:
                    if abs(v - self.nominal_v) > self.nominal_v * self.v_tolerance:
                        bits |= ALERT_VOLTAGE
                    if not (self.f_low <= f <= self.f_high):
                        bits |= ALERT_FREQUENCY
            elif self.breaker_tripped:
                bits = ALERT_VOLTAGE | ALERT_FREQUENCY
            if bits != self.alert_bits:
                self.alert_bits, ev.alerts_changed = bits, True

            # Warnings (no fault): low fuel, low oil
            warn = set()
            if self.fuel_pct < self.low_fuel_warn_pct:
                warn.add("low_fuel")
            if self.oil == 1:
                warn.add("low_oil")
            if frozenset(warn) != self._warnings:
                self._warnings, ev.warnings_changed = frozenset(warn), True
        return ev

    # ---- interlock ------------------------------------------------------------------
    def safety_check(self) -> tuple[bool, str, int]:
        """Pre-start / reset interlock. Returns (ok, reason, fault_bits)."""
        with self._lock:
            if self.oil >= 2:
                return False, "Low Oil Level Warning", FAULT_LOW_OIL
            if self.fuel_pct < self.low_fuel_interlock_pct:
                return False, "Fuel Level Below Threshold", FAULT_LOW_FUEL
            if self.temp > self.restart_temp_c:
                return False, f"Engine too hot to start ({self.temp:.0f} C)", FAULT_OVER_TEMP
            rest = self.weak_battery_v if self.weak_battery else self.battery_v
            if rest < self.min_start_v:
                return False, f"Starter battery too low ({rest:.1f} V)", FAULT_LOW_BATTERY
            return True, "", 0

    # ---- triggers (used by tests now, hotkeys and scenarios in PR 2) ---------------
    def set_load_pct(self, pct: float) -> None:
        with self._lock:
            self.load = max(0.0, pct / 100)

    def set_fuel_pct(self, pct: float) -> None:
        with self._lock:
            self.fuel_l = self.tank_l * max(0.0, min(100.0, pct)) / 100

    def set_oil(self, level: int) -> None:
        with self._lock:
            self.oil = max(0, min(2, int(level)))

    def set_overheat(self, on: bool) -> None:
        with self._lock:
            self.overheat = bool(on)

    def set_weak_battery(self, on: bool) -> None:
        with self._lock:
            self.weak_battery = bool(on)

    def reset_breaker(self) -> tuple[bool, str]:
        """Close the output breaker again; refused while still overloaded."""
        with self._lock:
            if not self.breaker_tripped:
                return True, "breaker not tripped"
            if self.load > self.max_load:
                return False, "Still overloaded: reduce load first"
            self.breaker_tripped, self._overload_s = False, 0.0
            return True, "breaker reset"

    # ---- readings -------------------------------------------------------------------
    def _jitter(self, value: float, spread: float) -> float:
        return value + self.rng.uniform(-spread, spread) if self.noise else value

    def read(self, run_state: int = OFF) -> SensorReading:
        with self._lock:
            if self.noise:
                self.rssi = max(-95, min(-45, self.rssi + self.rng.choice((-1, 0, 0, 1))))
            v, i, f = self._output(run_state)
            vb = self.battery_v
            if run_state == CRANK:  # the starter draws the battery down at once, not on the next tick
                vb = (self.weak_battery_v if self.weak_battery else self.battery_rest) - self.crank_drop
            return SensorReading(
                v=round(self._jitter(v, 1.5) if v else 0.0, 1),
                i=round(max(0.0, self._jitter(i, 0.2)) if i else 0.0, 2),
                f=round(self._jitter(f, 0.1) if f else 0.0, 2),
                fuel=round(max(0.0, min(100.0, self._jitter(self.fuel_pct, 0.1) if self.fuel_pct else 0.0)), 1),
                oil=self.oil,
                t=round(self._jitter(self.temp, 0.3), 1),
                vb=round(self._jitter(vb, 0.03), 2),
                rssi=self.rssi,
            )

    def extras(self) -> dict:
        """Model details for get_status (not part of the telemetry schema)."""
        with self._lock:
            return {
                "load_pct": round(self.load * 100, 1),
                "fuel_l": round(self.fuel_l, 2),
                "fuel_l_per_h": round(self.fuel_lph(), 2),
                "breaker_tripped": self.breaker_tripped,
                "alerts": self.alert_bits,
                "warnings": sorted(self._warnings),
            }
