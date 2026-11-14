"""Free-tier message budget guard.

IoT Hub F1 allows 8,000 messages per UTC day for the whole hub (telemetry and
direct-method calls both count). The guard counts what *this* simulator sends,
across all simulated devices and restarts, and refuses to send past `stop_at`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from .persist import StateFile

log = logging.getLogger(__name__)


def _utc_day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class BudgetGuard:
    def __init__(self, state_dir: Path, stop_at: int, daily_limit: int, today=_utc_day):
        self._file = StateFile(Path(state_dir) / "budget.json")
        self.stop_at = stop_at
        self.daily_limit = daily_limit
        self._today = today
        self._warned = False

    def _current(self, data: dict) -> int:
        return int(data.get("count", 0)) if data.get("day") == self._today() else 0

    @property
    def used(self) -> int:
        return self._current(self._file.load())

    def record(self, n: int = 1) -> None:
        """Count messages that are sent regardless of the guard (direct-method calls:
        IoT Hub bills them against the same daily quota)."""
        def _apply(data: dict) -> None:
            data["count"] = self._current(data) + n
            data["day"] = self._today()
        self._file.update(_apply)

    def try_consume(self, n: int = 1) -> bool:
        """Reserve n messages. Returns False (and sends nothing) when over budget."""
        allowed = {"ok": False}

        def _apply(data: dict) -> None:
            count = self._current(data)
            if count + n > self.stop_at:
                return
            data["day"] = self._today()
            data["count"] = count + n
            allowed["ok"] = True

        data = self._file.update(_apply)
        if not allowed["ok"]:
            if not self._warned:
                log.warning(
                    "Budget guard: %d/%d messages used today (stop_at=%d). "
                    "Telemetry paused until 00:00 UTC.",
                    self._current(data), self.daily_limit, self.stop_at,
                )
                self._warned = True
            return False
        self._warned = False
        return True
