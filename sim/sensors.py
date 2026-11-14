"""One set of sensor readings, in telemetry units.

The values come from the physics-lite generator model in sim/physics.py (M3).
"""

from __future__ import annotations

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
