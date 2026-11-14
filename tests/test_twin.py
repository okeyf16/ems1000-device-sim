"""M5: device twin - reported properties and desired settings."""

import threading
import time

import pytest

from fakes import FakeTimers
from sim import __version__
from sim.budget import BudgetGuard
from sim.config import SimConfig
from sim.device import VirtualDevice
from sim.state_machine import RunState
from sim.transport import DryRunTransport
from sim.twin import MODEL_NAME, SETTINGS


def make(tmp_path, desired=None, budget=None, interval=60, **sensors):
    cfg = SimConfig(devices=["ems-sim-01"], state_dir=tmp_path, sensors={"noise": False, **sensors})
    t = DryRunTransport("ems-sim-01")
    if desired:
        t.desired.update(desired)
    timers = FakeTimers()
    dev = VirtualDevice("ems-sim-01", cfg, t, budget, threading.Event(), interval=interval,
                        timer_factory=timers)
    return dev, t, timers


def started(tmp_path, **kw):
    dev, t, timers = make(tmp_path, **kw)
    dev.transport.connect()
    dev.twin.start()
    return dev, t, timers


# ---- start-up report ---------------------------------------------------------------------

def test_startup_reports_device_state_and_settings(tmp_path):
    dev, t, _ = started(tmp_path)
    r = t.reported
    assert r["device"]["model"] == MODEL_NAME and r["device"]["sim_version"] == __version__
    assert r["device"]["boot"] == dev.boot and r["device"]["started_at"].endswith("Z")
    assert (r["state"], r["run"], r["flt"]) == ("OFF", 0, 0)
    assert r["settings"] == {"telemetry_interval_s": 60, "load_pct": 25.0, "low_fuel_warn_pct": 10.0,
                             "overtemp_c": 95.0, "min_start_battery_v": 11.0, "speed": 1.0}
    assert "settings_status" not in r and len(t.reported_patches) == 1


def test_stored_desired_settings_override_config_at_startup(tmp_path, caplog):
    caplog.set_level("INFO")
    dev, t, _ = started(tmp_path, desired={"load_pct": 60, "telemetry_interval_s": 10, "$version": 7})
    assert dev.model.load == 0.6 and dev.interval == 10
    assert t.reported["settings"]["load_pct"] == 60.0
    assert t.reported["settings_status"]["load_pct"] == {"status": "applied", "desired_version": 7}
    assert "overrides config" in caplog.text


def test_stale_status_entries_are_cleared_at_startup(tmp_path):
    dev, t, _ = make(tmp_path)
    t.reported["settings_status"] = {"speed": {"status": "applied", "desired_version": 3}}
    dev.twin.start()
    assert "settings_status" not in t.reported


def test_twin_unavailable_does_not_stop_the_device(tmp_path, caplog):
    dev, t, _ = make(tmp_path)
    t.get_twin = lambda: (_ for _ in ()).throw(RuntimeError("twin down"))
    dev.twin.start()
    assert not dev.twin.enabled and t.reported_patches == []
    dev.generator.start()                     # state changes still work, just no twin reports
    assert t.reported_patches == []


# ---- state and fault reporting --------------------------------------------------------------

def test_state_changes_are_reported(tmp_path):
    dev, t, timers = started(tmp_path)
    t.invoke("start")
    timers.fire()
    assert t.reported["state"] == "RUNNING" and t.reported["run"] == 2
    states = [p["state"] for p in t.reported_patches[1:]]
    assert states == ["CRANKING", "RUNNING"]


def test_fault_reports_last_fault_and_it_survives_reset(tmp_path):
    dev, t, timers = started(tmp_path)
    t.invoke("start")
    timers.fire()
    dev.model.set_fuel_pct(0.01)
    dev.tick(600)
    lf = t.reported["last_fault"]
    assert t.reported["state"] == "FAULT" and lf["bits"] == 2 and "fuel" in lf["reason"].lower()
    dev.model.set_fuel_pct(100)
    assert t.invoke("reset_fault")[0] == 200
    assert t.reported["state"] == "OFF" and t.reported["last_fault"] == lf


def test_refused_start_reports_interlock_reason(tmp_path):
    dev, t, _ = started(tmp_path, oil_start=2)
    t.invoke("start")
    assert t.reported["last_fault"]["reason"] == "Low Oil Level Warning"


# ---- desired settings --------------------------------------------------------------------

@pytest.mark.parametrize("key,value,check", [
    ("telemetry_interval_s", 10, lambda d: d.interval == 10),
    ("load_pct", 80, lambda d: d.model.load == 0.8),
    ("low_fuel_warn_pct", 20, lambda d: d.model.low_fuel_warn_pct == 20),
    ("overtemp_c", 100, lambda d: d.model.overtemp_c == 100),
    ("min_start_battery_v", 11.5, lambda d: d.model.min_start_v == 11.5),
    ("speed", 60, lambda d: d.speed == 60),
])
def test_each_setting_applies_live_and_is_reported(tmp_path, key, value, check):
    dev, t, _ = started(tmp_path)
    t.update_desired({key: value})
    assert check(dev)
    assert t.reported["settings"][key] == value
    assert t.reported["settings_status"][key] == {"status": "applied", "desired_version": 2}


@pytest.mark.parametrize("key,value,reason", [
    ("telemetry_interval_s", 5, "between 10 and 3600"),
    ("telemetry_interval_s", 12.5, "whole number"),
    ("load_pct", 151, "between 0 and 150"),
    ("overtemp_c", 80, "between 85 and 120"),
    ("min_start_battery_v", "11", "must be a number"),
    ("speed", True, "must be a number"),
    ("colour", "red", "unknown setting"),
])
def test_bad_values_rejected_with_reason(tmp_path, key, value, reason):
    dev, t, _ = started(tmp_path)
    before = dict(t.reported["settings"])
    t.update_desired({key: value})
    st = t.reported["settings_status"][key]
    assert st["status"] == "rejected" and reason in st["reason"]
    assert t.reported["settings"] == before


def test_one_bad_value_does_not_block_the_others(tmp_path):
    dev, t, _ = started(tmp_path)
    t.update_desired({"load_pct": 50, "speed": 0})
    assert dev.model.load == 0.5 and dev.speed == 1
    assert t.reported["settings_status"]["load_pct"]["status"] == "applied"
    assert t.reported["settings_status"]["speed"]["status"] == "rejected"


def test_null_clears_setting_back_to_config_value(tmp_path):
    dev, t, _ = started(tmp_path)
    t.update_desired({"load_pct": 90})
    t.update_desired({"load_pct": None})
    assert dev.model.load == 0.25
    assert t.reported["settings"]["load_pct"] == 25.0 and "load_pct" not in t.reported["settings_status"]


def test_lowered_fuel_warning_threshold_changes_warning(tmp_path):
    dev, t, _ = started(tmp_path, fuel_start_pct=15)
    dev.tick(1)
    assert "low_fuel" not in dev.model.warnings
    t.update_desired({"low_fuel_warn_pct": 20})
    dev.tick(1)
    assert "low_fuel" in dev.model.warnings


def test_battery_threshold_changes_start_interlock(tmp_path):
    dev, t, _ = started(tmp_path)
    t.update_desired({"min_start_battery_v": 12.5})
    dev.model.battery_v = 12.4
    status, body = t.invoke("start")
    assert status == 409 and "battery" in body["reason"].lower()


def test_desired_patch_before_twin_started_is_applied_but_not_reported(tmp_path):
    dev, t, _ = make(tmp_path)
    t.update_desired({"load_pct": 40})          # e.g. arrives during connect
    assert dev.model.load == 0.4 and t.reported_patches == []


def test_interval_change_takes_effect_without_waiting_for_old_interval(tmp_path):
    dev, t, _ = make(tmp_path, interval=3600)
    worker = threading.Thread(target=dev.run, daemon=True)
    worker.start()
    time.sleep(0.3)
    assert len(t.sent) == 1
    t.update_desired({"telemetry_interval_s": 10})
    dev.interval = 0.2                          # test only: below the twin minimum to keep it fast
    dev.wake()
    time.sleep(0.7)
    dev.stop_event.set()
    worker.join(2)
    assert len(t.sent) >= 3


def test_twin_reports_count_against_budget(tmp_path):
    budget = BudgetGuard(tmp_path, stop_at=1, daily_limit=8000)
    dev, t, _ = started(tmp_path, budget=budget)
    assert len(t.reported_patches) == 1 and budget.used == 1
    t.update_desired({"load_pct": 30})          # budget exhausted: applied, not reported
    assert dev.model.load == 0.3 and len(t.reported_patches) == 1


def test_settings_table_ranges_match_plan():
    assert {k: (s.low, s.high) for k, s in SETTINGS.items()} == {
        "telemetry_interval_s": (10, 3600), "load_pct": (0, 150), "low_fuel_warn_pct": (1, 50),
        "overtemp_c": (85, 120), "min_start_battery_v": (10.0, 12.5), "speed": (1, 3600)}


def test_get_status_and_run_state_unaffected(tmp_path):
    dev, t, _ = started(tmp_path)
    assert t.invoke("get_status")[1]["state"] == "OFF" and dev.generator.state == RunState.OFF
