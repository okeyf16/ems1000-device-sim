"""Simulated sensor readings.

M2: readings depend on the run state with basic, steady values:
  OFF / STOP / FAULT : no AC output, engine at ambient, battery at rest
  CRANK              : starter draws the battery down (~10.6 V)
  RUN                : ~230 V / ~50 Hz output, steady load current, engine warm,
                       alternator charging the battery (~13.8 V)
M3 replaces this with the physics-lite model (fuel drain, heating curve, faults).
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class SensorReading:
    v: float      # AC line voltage, V
    i: float      # load current, A
    f: float      # AC frequency, Hz
    fuel: float   # fuel level, %
    oil: int      # 0=OK, 1=Low, 2=Critical
    t: float      # engine/ambient temperature, C
    vb: float     # starter battery, V
    rssi: int     # Wi-Fi/LTE signal, dBm


class SensorModel:
    def __init__(self, cfg: dict, rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self.noise = bool(cfg.get("noise", True))
        self.ambient = float(cfg.get("ambient_temp_c", 28.0))
        self.battery_rest = float(cfg.get("battery_rest_v", 12.6))
        self.fuel = float(cfg.get("fuel_start_pct", 85.0))
        self.rssi = int(cfg.get("rssi_start_dbm", -68))
        self.oil = 0
        # Basic running values (M2); M3 makes these evolve over time
        self.run_voltage = float(cfg.get("run_voltage_v", 230.0))
        self.run_freq = float(cfg.get("run_frequency_hz", 50.0))
        self.run_load = float(cfg.get("run_load_a", 8.0))
        self.run_temp = float(cfg.get("run_temp_c", 65.0))
        self.charge_v = float(cfg.get("charging_v", 13.8))
        self.crank_v = float(cfg.get("crank_battery_v", 10.6))
        self.low_fuel_pct = float(cfg.get("low_fuel_pct", 5.0))

    def _jitter(self, value: float, spread: float) -> float:
        return value + self.rng.uniform(-spread, spread) if self.noise else value

    def read(self, run_state: int = 0) -> SensorReading:
        """run_state: telemetry `run` code (0=Off, 1=Crank, 2=Run, 3=Stop, 4=Fault)."""
        # RSSI drifts slowly (random walk), clamped to a plausible range.
        if self.noise:
            self.rssi = max(-95, min(-45, self.rssi + self.rng.choice((-1, 0, 0, 1))))
        running = run_state == 2
        if running:
            v, f, i = self._jitter(self.run_voltage, 3.0), self._jitter(self.run_freq, 0.3), self._jitter(self.run_load, 0.5)
            t, vb = self._jitter(self.run_temp, 1.0), self._jitter(self.charge_v, 0.05)
        else:
            v = f = i = 0.0
            t = self._jitter(self.ambient, 0.3)
            vb = self._jitter(self.crank_v if run_state == 1 else self.battery_rest, 0.05 if run_state == 1 else 0.03)
        return SensorReading(
            v=round(v, 1),
            i=round(max(0.0, i), 2),
            f=round(f, 2),
            fuel=round(max(0.0, min(100.0, self._jitter(self.fuel, 0.2))), 1),
            oil=self.oil,
            t=round(t, 1),
            vb=round(vb, 2),
            rssi=self.rssi,
        )

    def safety_check(self) -> tuple[bool, str, int]:
        """Pre-start interlock, same rules as the firmware: refuse on low oil or empty tank.
        Returns (ok, reason, fault_bits); bits match the telemetry `flt` mask."""
        if self.oil != 0:
            return False, "Low Oil Level Warning", 1 << 0
        if self.fuel < self.low_fuel_pct:
            return False, "Fuel Level Below Threshold", 1 << 1
        return True, "", 0
