"""Direct-method handling: maps IoT Hub method names to generator actions.

Accepted names (firmware contract first, API/app aliases second):
  remote_start | start      -> start the generator
  remote_stop  | stop       -> stop it
  reset_fault               -> clear a fault lockout if the condition has gone
  get_status                -> current state + readings (no telemetry message used)
Anything else -> 404 unknown_method.

Every reply carries `cmd_id`: the API's `command_id` from the request payload when
present, else IoT Hub's request id.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .state_machine import Generator

log = logging.getLogger(__name__)

ALIASES = {
    "remote_start": "start",
    "start": "start",
    "remote_stop": "stop",
    "stop": "stop",
    "reset_fault": "reset_fault",
    "get_status": "get_status",
}


class CommandHandler:
    def __init__(self, generator: Generator, snapshot: Callable[[], dict],
                 on_command: Callable[[], None] | None = None):
        self.generator = generator
        self.snapshot = snapshot          # current readings for get_status
        self.on_command = on_command      # e.g. count the call against the budget

    def handle(self, method_name: str, payload: Any, request_id: str | None = None) -> tuple[int, dict]:
        cmd_id = payload.get("command_id") if isinstance(payload, dict) else None
        cmd_id = cmd_id or request_id
        action = ALIASES.get((method_name or "").strip())
        if self.on_command:
            self.on_command()

        if action == "start":
            status, body = self.generator.start()
        elif action == "stop":
            status, body = self.generator.stop()
        elif action == "reset_fault":
            status, body = self.generator.reset_fault()
        elif action == "get_status":
            status, body = 200, {
                "result": "ok",
                "state": self.generator.state_name,
                "run": int(self.generator.state),
                "flt": self.generator.flt,
                "readings": self.snapshot(),
            }
        else:
            status, body = 404, {"result": "unknown_method", "method": method_name,
                                 "supported": sorted(ALIASES)}

        body["cmd_id"] = cmd_id
        log.info("command %r (cmd_id=%s) -> %d %s", method_name, cmd_id, status, body.get("result"))
        return status, body
