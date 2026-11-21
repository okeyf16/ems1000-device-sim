"""Guards against burning the IoT Hub quota: one simulator per workspace, stop on a
reconnect storm or a quota 403, survive a single dropped connection, count twin traffic."""

import socket
import threading

import pytest

import cli
from fakes import FakeTimers
from sim.budget import BudgetGuard, blocks
from sim.config import SimConfig
from sim.device import VirtualDevice
from sim.instance import InstanceLock, port_free
from sim.transport import AzureSdkTransport, DryRunTransport, StormGuard, is_quota_error


class ServiceError(Exception):
    pass


class ClientError(Exception):
    pass


def quota_error():
    try:
        try:
            raise ServiceError("twin operation returned status 403")
        except ServiceError as inner:
            raise ClientError("Unexpected failure") from inner
    except ClientError as outer:
        return outer


class FlakyTransport(DryRunTransport):
    def __init__(self, device_id, error=None):
        super().__init__(device_id)
        self.error = error

    def send_telemetry(self, body, traceparent):
        if self.error:
            raise self.error
        super().send_telemetry(body, traceparent)


def make(tmp_path, transport, budget=None):
    cfg = SimConfig(devices=["ems-sim-01"], state_dir=tmp_path, sensors={"noise": False})
    stop = threading.Event()
    dev = VirtualDevice("ems-sim-01", cfg, transport, budget, stop, interval=60, timer_factory=FakeTimers())
    return dev, stop


# ---- single instance -------------------------------------------------------------------------

def test_second_simulator_is_refused(tmp_path):
    first, second = InstanceLock(tmp_path), InstanceLock(tmp_path)
    assert first.acquire()
    assert not second.acquire()
    assert second.other_pid is not None
    first.release()
    assert second.acquire()
    second.release()


def test_busy_port_is_detected():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert not port_free("127.0.0.1", port)
    assert port_free("127.0.0.1", port)
    assert port_free("127.0.0.1", 0)


def _config(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text(f"devices: [ems-sim-01]\nstate_dir: {tmp_path / 'state'}\n")
    return str(p)


def test_cli_stops_before_connecting_when_another_runs(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    held = InstanceLock(tmp_path / "state")
    assert held.acquire()
    monkeypatch.setattr(cli, "connection_string_for",
                        lambda *a, **k: pytest.fail("must not reach the connection step"))
    assert cli.main(["--config", cfg, "run", "--headless", "--count", "1"]) == 3
    held.release()


def test_cli_stops_when_the_panel_port_is_busy(tmp_path):
    cfg = _config(tmp_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert cli.main(["--config", cfg, "run", "--dry-run", "--headless", "--count", "1",
                         "--panel", "--panel-port", str(port)]) == 3


# ---- reconnect storm and quota ---------------------------------------------------------------

def test_storm_guard_trips_once():
    now = [0.0]
    g = StormGuard(drops=5, window_s=60, clock=lambda: now[0])
    assert [g.disconnected() for _ in range(4)] == [False] * 4
    assert g.disconnected() is True
    assert g.disconnected() is False          # reports only once


def test_slow_drops_never_trip():
    now = [0.0]
    g = StormGuard(drops=5, window_s=60, clock=lambda: now[0])
    for _ in range(20):
        now[0] += 30                          # one drop every 30 s: normal flakiness
        assert g.disconnected() is False


def test_transport_reports_a_storm_to_the_device():
    t = object.__new__(AzureSdkTransport)     # no SDK client needed for this path
    t.device_id = "ems-sim-01"
    t.storm = StormGuard()
    t._client = type("C", (), {"connected": False})()
    reasons = []
    t.on_fatal = reasons.append
    for _ in range(7):
        t._on_state_change()
    assert len(reasons) == 1 and "same device ID" in reasons[0]


def test_quota_errors_recognised_through_the_cause_chain():
    assert is_quota_error(quota_error())
    assert is_quota_error(Exception("IotHubQuotaExceeded"))
    assert not is_quota_error(Exception("Client is not connected to IoTHub"))
    assert not is_quota_error(None)


def test_quota_on_send_stops_the_run(tmp_path):
    dev, stop = make(tmp_path, FlakyTransport("ems-sim-01", quota_error()))
    assert dev.send_telemetry() is False
    assert stop.is_set() and "quota" in dev.fatal_reason


def test_dropped_connection_skips_one_heartbeat_and_carries_on(tmp_path):
    budget = BudgetGuard(tmp_path, stop_at=100, daily_limit=8000)
    t = FlakyTransport("ems-sim-01", ClientError("Client is not connected to IoTHub"))
    dev, stop = make(tmp_path, t, budget)
    assert dev.send_telemetry() is False
    assert not stop.is_set() and dev.fatal_reason is None
    assert budget.used == 0                   # the reserved message was given back
    t.error = None
    assert dev.send_telemetry() is True and budget.used == 1


def test_quota_on_twin_read_stops_the_run(tmp_path):
    t = DryRunTransport("ems-sim-01")
    t.get_twin = lambda: (_ for _ in ()).throw(quota_error())
    dev, stop = make(tmp_path, t)
    dev.twin.start()
    assert stop.is_set() and "quota" in dev.fatal_reason


# ---- budget counts twin traffic --------------------------------------------------------------

def test_twin_traffic_counted_in_half_kilobyte_blocks():
    assert blocks({"a": 1}) == 1
    assert blocks({"x": "y" * 1200}) == 3


def test_budget_refund_never_goes_negative(tmp_path):
    b = BudgetGuard(tmp_path, stop_at=10, daily_limit=8000)
    b.record(-5)
    assert b.used == 0
