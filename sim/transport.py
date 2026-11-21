"""Transport layer: how messages reach IoT Hub.

Everything above this module talks to `Transport`, so the official SDK can be
swapped for raw paho-mqtt later without touching device logic.
"""

from __future__ import annotations

import collections
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

# (method_name, payload, request_id) -> (status, response_body)
MethodHandler = Callable[[str, Any, str], tuple[int, dict]]
# desired-properties patch -> None (device twin, M5)
DesiredHandler = Callable[[dict], None]

log = logging.getLogger(__name__)

# More drops than this within the window means another client keeps taking over the
# device's connection (IoT Hub allows one per device ID): stop instead of fighting.
STORM_DROPS = 5
STORM_WINDOW_S = 60.0

QUOTA_MESSAGE = ("IoT Hub refused the device (403): most likely the free tier's daily message quota "
                 "is used up. It resets at 00:00 UTC (01:00 WAT). Stopping so nothing retries meanwhile.")
STORM_MESSAGE = ("IoT Hub dropped {device} {n} times in {window:.0f} s: another client is probably "
                 "connected with the same device ID (another simulator, Codespace or container). "
                 "Stopping to protect the daily message quota. Check: ps aux | grep '[c]li.py'")


def is_quota_error(exc: BaseException | None) -> bool:
    """Does this SDK error (or what caused it) say 403 / quota exceeded?"""
    seen = 0
    while exc is not None and seen < 5:
        text = str(exc).lower()
        if "403" in text or "quota" in text:
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


class StormGuard:
    """Counts disconnects; reports once when they come too fast."""

    def __init__(self, drops: int = STORM_DROPS, window_s: float = STORM_WINDOW_S, clock=time.monotonic):
        self.drops, self.window_s, self._clock = drops, window_s, clock
        self._times: collections.deque = collections.deque()
        self.tripped = False

    def disconnected(self) -> bool:
        """Record a drop; True the first time the limit is reached."""
        now = self._clock()
        self._times.append(now)
        while self._times and now - self._times[0] > self.window_s:
            self._times.popleft()
        if not self.tripped and len(self._times) >= self.drops:
            self.tripped = True
            return True
        return False


class Transport(ABC):
    # Set by the device: called once with a reason when the run must stop (storm, quota)
    on_fatal = None

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def send_telemetry(self, body: str, traceparent: str) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def set_method_handler(self, handler: MethodHandler) -> None:
        """Register the direct-method handler. Call before connect()."""

    # ---- device twin (M5) ----------------------------------------------------------
    @abstractmethod
    def set_desired_handler(self, handler: DesiredHandler) -> None:
        """Register the handler for desired-property patches. Call before connect()."""

    @abstractmethod
    def get_twin(self) -> dict:
        """Full twin: {"desired": {...,"$version": n}, "reported": {...}}."""

    @abstractmethod
    def patch_reported(self, patch: dict) -> None:
        """Merge-update reported properties (None values delete keys)."""


class AzureSdkTransport(Transport):
    """MQTT over TLS (port 8883) via azure-iot-device.

    content_type / content_encoding become $.ct / $.ce on the MQTT topic, exactly
    like the firmware's topic suffix, so IoT Hub routing and the ingest Function
    treat the body as UTF-8 JSON. traceparent travels as an application property.
    """

    def __init__(self, connection_string: str, device_id: str):
        from azure.iot.device import IoTHubDeviceClient  # imported lazily for dry runs

        self.device_id = device_id
        self._client = IoTHubDeviceClient.create_from_connection_string(
            connection_string, keep_alive=60
        )
        self._client.on_connection_state_change = self._on_state_change
        self.storm = StormGuard()

    def _on_state_change(self) -> None:
        if self._client.connected:
            log.info("[%s] IoT Hub connection connected", self.device_id)
            return
        if self.storm.disconnected():
            reason = STORM_MESSAGE.format(device=self.device_id, n=self.storm.drops, window=self.storm.window_s)
            log.error("[%s] %s", self.device_id, reason)
            if self.on_fatal:
                self.on_fatal(reason)
        elif not self.storm.tripped:
            log.info("[%s] IoT Hub connection DISCONNECTED (SDK will retry)", self.device_id)

    def set_method_handler(self, handler: MethodHandler) -> None:
        from azure.iot.device import MethodResponse

        def _on_method(request) -> None:
            try:
                status, body = handler(request.name, request.payload, request.request_id)
            except Exception:  # noqa: BLE001 - never leave IoT Hub waiting for a reply
                log.exception("[%s] method %s failed", self.device_id, request.name)
                status, body = 500, {"result": "error"}
            self._client.send_method_response(MethodResponse.create_from_method_request(request, status, body))

        self._client.on_method_request_received = _on_method

    def set_desired_handler(self, handler: DesiredHandler) -> None:
        def _on_patch(patch) -> None:
            try:
                handler(patch)
            except Exception:  # noqa: BLE001 - keep the SDK's handler thread alive
                log.exception("[%s] desired-properties patch failed", self.device_id)

        self._client.on_twin_desired_properties_patch_received = _on_patch

    def get_twin(self) -> dict:
        return self._client.get_twin()

    def patch_reported(self, patch: dict) -> None:
        self._client.patch_twin_reported_properties(patch)

    def connect(self) -> None:
        self._client.connect()

    def send_telemetry(self, body: str, traceparent: str) -> None:
        from azure.iot.device import Message

        msg = Message(body, content_encoding="utf-8", content_type="application/json")
        msg.custom_properties["traceparent"] = traceparent
        self._client.send_message(msg)

    def disconnect(self) -> None:
        try:
            self._client.shutdown()
        except Exception:  # noqa: BLE001 - best effort on exit
            log.debug("shutdown error ignored", exc_info=True)


class DryRunTransport(Transport):
    """Prints what would be sent. No network, no budget used."""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.sent: list[tuple[str, str]] = []
        self._handler: MethodHandler | None = None
        self._desired_handler: DesiredHandler | None = None
        self.desired: dict = {"$version": 1}
        self.reported: dict = {}
        self.reported_patches: list[dict] = []

    def connect(self) -> None:
        log.info("[%s] DRY RUN - not connecting to IoT Hub", self.device_id)

    def send_telemetry(self, body: str, traceparent: str) -> None:
        self.sent.append((body, traceparent))

    def disconnect(self) -> None:
        pass

    def set_method_handler(self, handler: MethodHandler) -> None:
        self._handler = handler

    def set_desired_handler(self, handler: DesiredHandler) -> None:
        self._desired_handler = handler

    def get_twin(self) -> dict:
        return {"desired": dict(self.desired), "reported": dict(self.reported)}

    def patch_reported(self, patch: dict) -> None:
        self.reported_patches.append(patch)
        _merge(self.reported, patch)
        log.info("[%s] DRY RUN twin reported: %s", self.device_id, patch)

    def update_desired(self, patch: dict) -> None:
        """Test hook: change desired properties as the portal/API would."""
        _merge(self.desired, patch)
        self.desired["$version"] = self.desired.get("$version", 1) + 1
        if self._desired_handler:
            self._desired_handler({**patch, "$version": self.desired["$version"]})

    def invoke(self, method_name: str, payload: Any = None, request_id: str = "dry-run") -> tuple[int, dict]:
        """Test hook: deliver a direct method as IoT Hub would."""
        if self._handler is None:
            raise RuntimeError("no method handler registered")
        return self._handler(method_name, payload, request_id)


def _merge(target: dict, patch: dict) -> None:
    """JSON merge-patch, as IoT Hub applies twin patches (None deletes)."""
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value
