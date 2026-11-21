"""Configuration loading: YAML file + environment (.env) for secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "default.yaml"


@dataclass
class SimConfig:
    devices: list[str]
    schema_version: int = 1
    heartbeat_seconds: int = 60
    max_payload_bytes: int = 512
    daily_limit: int = 8000
    stop_at: int = 7500
    sensors: dict = field(default_factory=dict)
    crank_seconds: float = 2.5
    stop_seconds: float = 3.0
    tick_seconds: float = 1.0      # real seconds between model steps
    speed: float = 1.0             # simulated seconds per real second (--speed)
    state_dir: Path = Path(".state")
    panel_port: int = 8080
    panel_api_url: str | None = None   # staging API for the panel's cloud view
    panel_api_key: str | None = None   # X-API-Key for that API (env EMS_API_KEY only, never in YAML)


def load_config(path: str | Path | None = None) -> SimConfig:
    load_dotenv()  # reads .env in the working directory, if present
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    tel = raw.get("telemetry", {})
    bud = raw.get("budget", {})
    devices = raw.get("devices") or []
    if not devices:
        raise ValueError("config: 'devices' must list at least one device id")

    return SimConfig(
        devices=[str(d) for d in devices],
        schema_version=int(tel.get("schema_version", 1)),
        heartbeat_seconds=int(tel.get("heartbeat_seconds", 60)),
        max_payload_bytes=int(tel.get("max_payload_bytes", 512)),
        daily_limit=int(bud.get("daily_limit", 8000)),
        stop_at=int(bud.get("stop_at", 7500)),
        sensors=raw.get("sensors", {}) or {},
        crank_seconds=float((raw.get("generator") or {}).get("crank_seconds", 2.5)),
        stop_seconds=float((raw.get("generator") or {}).get("stop_seconds", 3.0)),
        tick_seconds=float((raw.get("simulation") or {}).get("tick_seconds", 1.0)),
        speed=float((raw.get("simulation") or {}).get("speed", 1.0)),
        state_dir=Path(raw.get("state_dir", ".state")),
        panel_port=int((raw.get("panel") or {}).get("port", 8080)),
        panel_api_url=os.getenv("EMS_API_URL") or (raw.get("panel") or {}).get("api_url") or None,
        panel_api_key=os.getenv("EMS_API_KEY") or None,
    )


def connection_string_for(device_id: str, single_device: bool) -> str | None:
    """Per-device variable first (IOTHUB_CONN_EMS_SIM_01), then the generic one
    when only one device is configured."""
    key = "IOTHUB_CONN_" + device_id.upper().replace("-", "_")
    value = os.getenv(key)
    if not value and single_device:
        value = os.getenv("IOTHUB_DEVICE_CONNECTION_STRING")
    if value and f"DeviceId={device_id};" not in value + ";":
        raise ValueError(
            f"Connection string for {device_id} has a different DeviceId - check {key}"
        )
    return value
