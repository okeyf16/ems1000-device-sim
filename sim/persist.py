"""Tiny JSON-file persistence for values that must survive restarts
(boot counter, daily message count) - mirrors the ESP32's NVS/RTC memory."""

from __future__ import annotations

import json
import threading
from pathlib import Path

_lock = threading.Lock()


class StateFile:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.path)

    def update(self, fn) -> dict:
        with _lock:
            data = self.load()
            fn(data)
            self.save(data)
            return data


def next_boot_count(state_dir: Path, device_id: str) -> int:
    """Increment and return the device's boot counter (like `bootCount` in firmware)."""
    sf = StateFile(Path(state_dir) / f"{device_id}.json")
    data = sf.update(lambda d: d.__setitem__("boot", int(d.get("boot", 0)) + 1))
    return data["boot"]
