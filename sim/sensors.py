"""Simulated sensor readings.

M1: the generator is always OFF, so readings are the 'parked' baseline:
no AC output, engine at ambient temperature, starter battery at rest.
M3 replaces this with the physics-lite model (load, fuel drain, heating, crank dip).
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

    def _jitter(self, value: float, spread: float) -> float:
        return value + self.rng.uniform(-spread, spread) if self.noise else value

    def read(self) -> SensorReading:
        # RSSI drifts slowly (random walk), clamped to a plausible range.
        if self.noise:
            self.rssi = max(-95, min(-45, self.rssi + self.rng.choice((-1, 0, 0, 1))))
        return SensorReading(
            v=0.0,
            i=0.0,
            f=0.0,
            fuel=round(max(0.0, min(100.0, self._jitter(self.fuel, 0.2))), 1),
            oil=self.oil,
            t=round(self._jitter(self.ambient, 0.3), 1),
            vb=round(self._jitter(self.battery_rest, 0.03), 2),
            rssi=self.rssi,
        )
