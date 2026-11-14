"""Transport layer: how messages reach IoT Hub.

Everything above this module talks to `Transport`, so the official SDK can be
swapped for raw paho-mqtt later without touching device logic.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

# (method_name, payload, request_id) -> (status, response_body)
MethodHandler = Callable[[str, Any, str], tuple[int, dict]]

log = logging.getLogger(__name__)


class Transport(ABC):
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def send_telemetry(self, body: str, traceparent: str) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def set_method_handler(self, handler: MethodHandler) -> None:
        """Register the direct-method handler. Call before connect()."""


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

    def _on_state_change(self) -> None:
        state = "connected" if self._client.connected else "DISCONNECTED (SDK will retry)"
        log.info("[%s] IoT Hub connection %s", self.device_id, state)

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

    def connect(self) -> None:
        log.info("[%s] DRY RUN - not connecting to IoT Hub", self.device_id)

    def send_telemetry(self, body: str, traceparent: str) -> None:
        self.sent.append((body, traceparent))

    def disconnect(self) -> None:
        pass

    def set_method_handler(self, handler: MethodHandler) -> None:
        self._handler = handler

    def invoke(self, method_name: str, payload: Any = None, request_id: str = "dry-run") -> tuple[int, dict]:
        """Test hook: deliver a direct method as IoT Hub would."""
        if self._handler is None:
            raise RuntimeError("no method handler registered")
        return self._handler(method_name, payload, request_id)
