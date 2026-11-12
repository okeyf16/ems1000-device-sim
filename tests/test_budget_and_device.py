import json
import threading

from sim.budget import BudgetGuard
from sim.config import SimConfig, connection_string_for
from sim.device import VirtualDevice
from sim.transport import DryRunTransport


def test_budget_stops_at_limit_and_persists(tmp_path):
    g = BudgetGuard(tmp_path, stop_at=3, daily_limit=8000, today=lambda: "2026-11-12")
    assert [g.try_consume() for _ in range(4)] == [True, True, True, False]
    # a restart (new guard object) still sees today's count
    g2 = BudgetGuard(tmp_path, stop_at=3, daily_limit=8000, today=lambda: "2026-11-12")
    assert g2.used == 3 and g2.try_consume() is False


def test_budget_resets_on_new_utc_day(tmp_path):
    day = {"d": "2026-11-12"}
    g = BudgetGuard(tmp_path, stop_at=1, daily_limit=8000, today=lambda: day["d"])
    assert g.try_consume() and not g.try_consume()
    day["d"] = "2026-11-13"
    assert g.used == 0 and g.try_consume()


def _cfg(tmp_path):
    return SimConfig(devices=["ems-sim-01"], state_dir=tmp_path,
                     sensors={"noise": False})


def test_device_sends_valid_sequenced_messages_and_counts_boots(tmp_path):
    cfg = _cfg(tmp_path)
    t = DryRunTransport("ems-sim-01")
    dev = VirtualDevice("ems-sim-01", cfg, t, None, threading.Event(), interval=0)
    dev.run(max_messages=3)
    bodies = [json.loads(b) for b, _ in t.sent]
    assert [b["seq"] for b in bodies] == [1, 2, 3]
    assert all(b["boot"] == 1 and b["run"] == 0 and b["id"] == "ems-sim-01" for b in bodies)

    dev2 = VirtualDevice("ems-sim-01", cfg, DryRunTransport("ems-sim-01"), None,
                         threading.Event(), interval=0)
    assert dev2.boot == 2  # boot counter survives restart, seq restarts


def test_device_respects_budget(tmp_path):
    cfg = _cfg(tmp_path)
    budget = BudgetGuard(tmp_path, stop_at=2, daily_limit=8000)
    t = DryRunTransport("ems-sim-01")
    dev = VirtualDevice("ems-sim-01", cfg, t, budget, threading.Event(), interval=0)
    assert [dev.send_telemetry() for _ in range(3)] == [True, True, False]
    assert len(t.sent) == 2


def test_connection_string_lookup(monkeypatch):
    good = "HostName=h.azure-devices.net;DeviceId=ems-sim-01;SharedAccessKey=abc="
    monkeypatch.setenv("IOTHUB_DEVICE_CONNECTION_STRING", good)
    assert connection_string_for("ems-sim-01", single_device=True) == good
    assert connection_string_for("ems-sim-01", single_device=False) is None
    monkeypatch.setenv("IOTHUB_CONN_EMS_SIM_02", good)  # wrong DeviceId
    try:
        connection_string_for("ems-sim-02", single_device=False)
        raise AssertionError("expected mismatch error")
    except ValueError:
        pass
