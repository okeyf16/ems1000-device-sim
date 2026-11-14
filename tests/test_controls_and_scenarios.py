"""M3 PR 2: local controls (keyboard shortcuts), scenarios and --speed."""

import json
import threading

import pytest

import cli
from fakes import FakeTimers
from sim.config import SimConfig
from sim.controls import ACTION_NAMES, HOTKEYS, Controls, handle_key, help_text
from sim.device import VirtualDevice
from sim.physics import FAULT_LOW_FUEL
from sim.scenario import ScenarioRunner, available, load_scenario
from sim.state_machine import RunState
from sim.transport import DryRunTransport


def make(tmp_path, timers=None, **kw):
    sensors = {"noise": False, **kw.pop("sensors", {})}
    cfg = SimConfig(devices=["ems-sim-01"], state_dir=tmp_path, sensors=sensors, **kw)
    t = DryRunTransport("ems-sim-01")
    stop = threading.Event()
    dev = VirtualDevice("ems-sim-01", cfg, t, None, stop, interval=3600,
                        **({"timer_factory": timers} if timers else {}))
    return dev, t, stop, Controls(dev)


# ---- controls / keyboard ---------------------------------------------------------------

def test_every_hotkey_maps_to_an_action_and_is_in_help():
    text = help_text()
    for action, label in HOTKEYS.values():
        assert action in ACTION_NAMES and label in text
    assert set(HOTKEYS) == set("sxr+-ofeFhbpti")


def test_start_stop_reset_keys_use_the_state_machine(tmp_path):
    timers = FakeTimers()
    dev, t, _, c = make(tmp_path, timers)
    assert handle_key(c, "s").endswith("200 accepted")
    timers.fire()
    assert dev.generator.state == RunState.RUN
    assert "400 refused" in handle_key(c, "s")
    assert handle_key(c, "x").endswith("200 accepted")
    assert "no_fault" in handle_key(c, "r")
    assert handle_key(c, "z") is None and "Keyboard shortcuts" in handle_key(c, "?")


def test_load_keys_step_10_percent_and_warn_about_overload(tmp_path):
    dev, *_, c = make(tmp_path)
    assert "load 35 %" in handle_key(c, "+")
    assert "load 45 %" in handle_key(c, "=")           # unshifted +
    assert "load 35 %" in handle_key(c, "-")
    assert "breaker trips" in c.do("load", 110)
    assert "alerts" in c.do("load", 104)
    assert c.do("load", -5).startswith("load 0 %")


def test_oil_fuel_overheat_battery_keys(tmp_path):
    dev, *_, c = make(tmp_path)
    m = dev.model
    assert [handle_key(c, "o") and m.oil for _ in range(3)] == [1, 2, 0]
    handle_key(c, "f")
    assert round(m.fuel_pct) == 9
    handle_key(c, "e")
    assert m.fuel_pct == 0
    handle_key(c, "F")
    assert m.fuel_pct == 100
    handle_key(c, "h")
    assert m.overheat
    handle_key(c, "h")
    assert not m.overheat
    handle_key(c, "b")
    assert m.weak_battery
    assert c.do("weak_battery", False) == "weak battery off"
    assert c.do("overheat", "on").startswith("overheat ON")


def test_breaker_key_refused_while_overloaded_then_resets_and_sends(tmp_path):
    timers = FakeTimers()
    dev, t, _, c = make(tmp_path, timers)
    c.do("start")
    timers.fire()
    c.do("load", 110)
    for _ in range(6):
        dev.tick(1)
    assert dev.model.breaker_tripped
    assert "refused" in handle_key(c, "p")
    c.do("load", 50)
    sent = len(t.sent)
    assert handle_key(c, "p").endswith("breaker reset")
    assert len(t.sent) == sent + 1 and json.loads(t.sent[-1][0])["v"] > 200


def test_send_and_status_keys(tmp_path):
    dev, t, _, c = make(tmp_path)
    assert "telemetry sent" in handle_key(c, "t") and len(t.sent) == 1
    status = handle_key(c, "i")
    assert "OFF" in status and "speed 1x" in status and "breaker ok" in status


def test_unknown_action_rejected(tmp_path):
    *_, c = make(tmp_path)
    with pytest.raises(ValueError):
        c.do("explode")


# ---- speed -----------------------------------------------------------------------------

def test_speed_scales_model_time_only(tmp_path):
    dev, t, stop, _ = make(tmp_path, speed=600.0, tick_seconds=0.01)
    assert dev.speed == 600
    dev.model.set_fuel_pct(100)
    dev.generator.state = RunState.RUN            # run without cranking for this check
    th = threading.Thread(target=dev._ticker, daemon=True)
    th.start()
    stop.wait(0.5)                                # ~0.5 s real -> ~300 s engine
    stop.set()
    th.join(1)
    burned_l = 15 - dev.model.fuel_l
    assert 0.03 < burned_l < 0.2                  # 0.9 L/h for ~300 s = ~0.075 L


@pytest.mark.parametrize("argv,err", [
    (["run", "--dry-run", "--speed", "0"], "--speed"),
    (["run", "--dry-run", "--speed", "5000"], "--speed"),
    (["run", "--dry-run", "--scenario", "nope"], "not found"),
])
def test_cli_rejects_bad_options(argv, err, caplog):
    assert cli.main(argv) == 2
    assert err in caplog.text


def test_cli_lists_scenarios(capsys):
    assert cli.main(["scenarios"]) == 0
    out = capsys.readouterr().out
    for name in ("normal-day", "out-of-fuel", "overload", "overheat"):
        assert name in out


# ---- scenarios -------------------------------------------------------------------------

def test_shipped_scenarios_load():
    assert available() == ["normal-day", "out-of-fuel", "overheat", "overload"]
    for name in available():
        sc = load_scenario(name)
        assert sc.steps and sc.speed


@pytest.mark.parametrize("body,err", [
    ("steps: []", "non-empty"),
    ("steps:\n  - do: explode", "unknown action"),
    ("steps:\n  - do: start\n    wait: 3", "exactly one"),
    ("steps:\n  - until: {state: FLYING}", "state must be"),
    ("steps:\n  - until: {colour: red}", "conditions"),
    ("steps:\n  - do: start\n    when: now", "unknown keys"),
])
def test_bad_scenarios_rejected(tmp_path, body, err):
    p = tmp_path / "bad.yaml"
    p.write_text(body)
    with pytest.raises(ValueError, match=err):
        load_scenario(str(p))


def run_scenario(tmp_path, name, speed=3600.0):
    sc = load_scenario(name)
    dev, t, stop, c = make(tmp_path, speed=speed, tick_seconds=0.005, crank_seconds=0.05,
                           stop_seconds=0.05, sensors=sc.setup)
    worker = threading.Thread(target=dev.run, daemon=True)
    worker.start()
    runner = ScenarioRunner(sc, c, stop, poll_s=0.005)
    try:
        ok = runner.run()
    finally:
        stop.set()
        worker.join(2)
    return ok, dev, [json.loads(b) for b, _ in t.sent]


def test_out_of_fuel_scenario_end_to_end(tmp_path):
    ok, dev, msgs = run_scenario(tmp_path, "out-of-fuel")
    assert ok
    runs = [m["run"] for m in msgs]
    assert 4 in runs and runs[-1] == 0                       # faulted, then restarted and stopped
    stall = next(m for m in msgs if m["run"] == 4)
    assert stall["flt"] & FAULT_LOW_FUEL and stall["fuel"] == 0
    assert dev.model.fuel_pct > 90                           # refuelled


def test_overheat_scenario_end_to_end(tmp_path):
    ok, dev, msgs = run_scenario(tmp_path, "overheat")
    assert ok and any(m["run"] == 4 and m["flt"] & 4 for m in msgs)
    assert dev.generator.state == RunState.OFF


def test_overload_scenario_end_to_end(tmp_path):
    ok, dev, msgs = run_scenario(tmp_path, "overload", speed=50.0)
    assert ok and any(m["v"] == 0 and m["run"] == 2 and m["flt"] == 48 for m in msgs)
    assert not dev.model.breaker_tripped


def test_normal_day_scenario_end_to_end(tmp_path):
    ok, dev, msgs = run_scenario(tmp_path, "normal-day")
    assert ok and [m["run"] for m in msgs][-1] == 0 and dev.model.load == 0.25


def test_until_timeout_has_a_real_time_floor_for_cranking(tmp_path):
    """At high speed a 60 engine-second timeout is milliseconds; cranking still takes real time."""
    p = tmp_path / "t.yaml"
    p.write_text("steps:\n  - do: start\n  - until: {state: RUNNING}\n    timeout: 60\n")
    dev, t, stop, c = make(tmp_path, speed=3600.0, crank_seconds=0.3)
    assert ScenarioRunner(load_scenario(str(p)), c, stop, poll_s=0.01).run() is True
    dev.generator.shutdown()


def test_until_timeout_stops_scenario(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("steps:\n  - until: {state: RUNNING}\n    timeout: 1\n  - do: start\n")
    dev, t, stop, c = make(tmp_path, speed=1.0)
    runner = ScenarioRunner(load_scenario(str(p)), c, stop, poll_s=0.01, min_real_timeout_s=0.05)
    assert runner.run() is False and dev.generator.state == RunState.OFF
