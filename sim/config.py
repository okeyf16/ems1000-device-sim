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
    state_dir: Path = Path(".state")


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
        state_dir=Path(raw.get("state_dir", ".state")),
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
