"""Telemetry payload builder + contract validation.

Contract: wokwi-simulation-code spec section 2 (schema v1, < 512 bytes).
Field order matches the firmware so payloads diff cleanly against Wokwi output.
"""

from __future__ import annotations

import json
import secrets
import time

from .sensors import SensorReading

# field -> allowed python types
SCHEMA_V1: dict[str, tuple[type, ...]] = {
    "sv": (int,), "id": (str,), "ts": (int,), "boot": (int,), "seq": (int,),
    "v": (int, float), "i": (int, float), "f": (int, float), "fuel": (int, float),
    "oil": (int,), "t": (int, float), "vb": (int, float), "flt": (int,),
    "run": (int,), "rssi": (int,),
}

RUN_STATES = {0: "OFF", 1: "CRANK", 2: "RUN", 3: "STOP", 4: "FAULT"}


class PayloadError(ValueError):
    pass


def build_payload(*, schema_version: int, device_id: str, boot: int, seq: int,
                  reading: SensorReading, flt: int, run: int,
                  ts_ms: int | None = None) -> dict:
    return {
        "sv": schema_version,
        "id": device_id,
        "ts": ts_ms if ts_ms is not None else int(time.time() * 1000),
        "boot": boot,
        "seq": seq,
        "v": reading.v,
        "i": reading.i,
        "f": reading.f,
        "fuel": reading.fuel,
        "oil": reading.oil,
        "t": reading.t,
        "vb": reading.vb,
        "flt": flt,
        "run": run,
        "rssi": reading.rssi,
    }


def validate(payload: dict, max_bytes: int = 512) -> str:
    """Check fields, types and ranges; return the compact JSON string to send."""
    missing = SCHEMA_V1.keys() - payload.keys()
    extra = payload.keys() - SCHEMA_V1.keys()
    if missing or extra:
        raise PayloadError(f"schema mismatch: missing={sorted(missing)} extra={sorted(extra)}")
    for key, types in SCHEMA_V1.items():
        val = payload[key]
        if isinstance(val, bool) or not isinstance(val, types):
            raise PayloadError(f"{key}={val!r} has wrong type")
    if payload["oil"] not in (0, 1, 2):
        raise PayloadError(f"oil={payload['oil']} not in 0/1/2")
    if payload["run"] not in RUN_STATES:
        raise PayloadError(f"run={payload['run']} not in 0..4")
    if not 0 <= payload["fuel"] <= 100:
        raise PayloadError(f"fuel={payload['fuel']} outside 0..100")
    if payload["flt"] < 0:
        raise PayloadError("flt must be a non-negative bitmask")

    body = json.dumps(payload, separators=(",", ":"))
    size = len(body.encode("utf-8"))
    if size >= max_bytes:
        raise PayloadError(f"payload is {size} bytes, limit is < {max_bytes}")
    return body


def new_traceparent() -> str:
    """W3C trace context header, same shape the firmware generates."""
    return f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
