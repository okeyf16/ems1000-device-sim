"""M3 PR 1: ECO4990ES physics-lite model and automatic faults."""

import json
import threading

import pytest

from fakes import FakeTimers
from sim.config import SimConfig
from sim.device import VirtualDevice
from sim.physics import (
    ALERT_FREQUENCY,
    ALERT_VOLTAGE,
    CRANK,
    FAULT_LOW_BATTERY,
    FAULT_LOW_FUEL,
    FAULT_LOW_OIL,
    FAULT_OVER_TEMP,
    OFF,
    RUN,
    GeneratorModel,
)
from sim.state_machine import RunState
from sim.transport import DryRunTransport


def model(**cfg):
    return GeneratorModel({"noise": False, **cfg})


def hours_until_empty(m, load_pct, dt=60.0):
    m.set_load_pct(load_pct)
    m.set_fuel_pct(100)
    t = 0.0
    while m.fuel_l > 0:
        m.step(dt, RUN)
        t += dt
    return t / 3600


# ---- physics ---------------------------------------------------------------------------

def test_nameplate_derived_values():
    m = model()
    assert round(m.rated_a, 1) == 13.6 and round(m.max_load, 3) == 1.067


def test_fuel_runtime_matches_spec_12h_at_50pct():
    assert hours_until_empty(model(), 50) == pytest.approx(12.0, rel=0.05)


def test_fuel_runtime_other_loads():
    assert hours_until_empty(model(), 25) == pytest.approx(16.7, rel=0.05)
    assert hours_until_empty(model(), 100) == pytest.approx(7.7, rel=0.05)


def test_no_fuel_used_when_not_running():
    m = model()
    before = m.fuel_l
    for state in (OFF, CRANK):
        m.step(3600, state)
    assert m.fuel_l == before


def test_engine_heats_towards_load_target_and_cools_when_off():
    m = model(load_pct=25)
    for _ in range(60):                       # 60 min running
        m.step(60, RUN)
    assert m.temp == pytest.approx(28 + 35 + 25 * 0.25, abs=1.0)   # ~69 C
    for _ in range(90):                       # 90 min off
        m.step(60, OFF)
    assert m.temp < 32


def test_battery_crank_dip_and_charging():
    m = model()
    assert m.read(CRANK).vb == 10.6
    for _ in range(30):
        m.step(60, RUN)
    assert m.read(RUN).vb == pytest.approx(13.8, abs=0.05)


def test_output_at_default_load_and_governor_droop():
    m = model()
    r = m.read(RUN)
    assert (r.v, r.f, r.i) == (218.5, 51.12, 3.41)
    m.set_load_pct(100)
    r = m.read(RUN)
    assert (r.v, r.f) == (214.0, 50.0)
    assert m.read(OFF).v == 0.0 and m.read(OFF).f == 0.0


def test_over_rated_but_under_max_sets_frequency_alert_only():
    m = model(load_pct=106)
    ev = m.step(1, RUN)
    assert ev.alerts_changed and m.alert_bits == ALERT_FREQUENCY and not m.breaker_tripped


def test_overload_breaker_trips_after_5s_and_resets_only_when_load_reduced():
    m = model(load_pct=110)
    for _ in range(4):
        assert not m.step(1, RUN).breaker_tripped
    assert m.step(1, RUN).breaker_tripped
    r = m.read(RUN)
    assert (r.v, r.i) == (0.0, 0.0) and r.f > 0          # engine still running, outlets dead
    assert m.alert_bits == ALERT_VOLTAGE | ALERT_FREQUENCY
    assert m.reset_breaker() == (False, "Still overloaded: reduce load first")
    m.set_load_pct(50)
    assert m.reset_breaker()[0]
    m.step(1, RUN)
    assert m.alert_bits == 0 and m.read(RUN).v > 200


def test_warnings_low_fuel_and_low_oil():
    m = model()
    m.set_fuel_pct(9)
    ev = m.step(1, OFF)
    assert ev.warnings_changed and m.warnings == {"low_fuel"}
    m.set_oil(1)
    assert m.step(1, OFF).warnings_changed and m.warnings == {"low_fuel", "low_oil"}
    assert not m.step(1, OFF).warnings_changed            # only on change


@pytest.mark.parametrize("setup,bit,reason", [
    (lambda m: m.set_oil(2), FAULT_LOW_OIL, "Low Oil"),
    (lambda m: m.set_fuel_pct(4), FAULT_LOW_FUEL, "Fuel Level Below Threshold"),
    (lambda m: setattr(m, "temp", 85.0), FAULT_OVER_TEMP, "too hot"),
    (lambda m: m.set_weak_battery(True), FAULT_LOW_BATTERY, "battery too low"),
])
def test_interlock_refusals(setup, bit, reason):
    m = model()
    setup(m)
    ok, why, bits = m.safety_check()
    assert not ok and bits == bit and reason.lower() in why.lower()


def test_low_oil_level_1_is_warning_only_and_start_allowed():
    m = model()
    m.set_oil(1)
    assert m.safety_check()[0] is True


# ---- automatic faults on the device ------------------------------------------------

def device(tmp_path, **sensors):
    cfg = SimConfig(devices=["ems-sim-01"], state_dir=tmp_path, sensors={"noise": False, **sensors})
    transport, timers = DryRunTransport("ems-sim-01"), FakeTimers()
    dev = VirtualDevice("ems-sim-01", cfg, transport, None, threading.Event(), interval=0, timer_factory=timers)
    return dev, transport, timers


def running_device(tmp_path, **sensors):
    dev, transport, timers = device(tmp_path, **sensors)
    assert transport.invoke("start")[0] == 200
    timers.fire()
    assert dev.generator.state == RunState.RUN
    return dev, transport, timers


def last(transport):
    return json.loads(transport.sent[-1][0])


def test_running_out_of_fuel_stalls_then_refuel_and_reset(tmp_path):
    dev, transport, timers = running_device(tmp_path)
    dev.model.set_fuel_pct(0.01)
    dev.tick(600)
    assert dev.generator.state == RunState.FAULT
    assert last(transport)["run"] == 4 and last(transport)["flt"] & FAULT_LOW_FUEL
    assert transport.invoke("reset_fault")[0] == 409          # still empty
    dev.model.set_fuel_pct(100)
    status, body = transport.invoke("reset_fault")
    assert (status, body["result"]) == (200, "cleared")
    assert transport.invoke("start")[0] == 200


def test_oil_critical_while_running_triggers_low_oil_protection(tmp_path):
    dev, transport, _ = running_device(tmp_path)
    dev.model.set_oil(2)
    dev.tick(1)
    assert dev.generator.state == RunState.FAULT and dev.generator.flt & FAULT_LOW_OIL


def test_overheat_trips_and_reset_waits_for_cool_down(tmp_path):
    dev, transport, _ = running_device(tmp_path)
    dev.model.set_overheat(True)
    for _ in range(30):
        dev.tick(60)
        if dev.generator.state == RunState.FAULT:
            break
    assert dev.generator.state == RunState.FAULT and dev.generator.flt & FAULT_OVER_TEMP
    dev.model.set_overheat(False)
    status, body = transport.invoke("reset_fault")
    assert status == 409 and "too hot" in body["reason"]
    for _ in range(60):                                       # an hour cooling
        dev.tick(60)
    assert transport.invoke("reset_fault")[0] == 200


def test_weak_battery_start_refused_with_bit3(tmp_path):
    dev, transport, _ = device(tmp_path)
    dev.model.set_weak_battery(True)
    status, body = transport.invoke("start")
    assert status == 409 and dev.generator.flt & FAULT_LOW_BATTERY


def test_overload_trip_sends_telemetry_and_engine_keeps_running(tmp_path):
    dev, transport, _ = running_device(tmp_path)
    sent_before = len(transport.sent)
    dev.model.set_load_pct(110)
    for _ in range(6):
        dev.tick(1)
    assert dev.generator.state == RunState.RUN
    assert len(transport.sent) > sent_before
    msg = last(transport)
    assert msg["v"] == 0.0 and msg["i"] == 0.0 and msg["flt"] & (ALERT_VOLTAGE | ALERT_FREQUENCY)


def test_low_fuel_warning_sends_one_message(tmp_path):
    dev, transport, _ = device(tmp_path)
    dev.model.set_fuel_pct(9)
    dev.tick(1)
    dev.tick(1)
    assert len(transport.sent) == 1 and last(transport)["fuel"] == 9.0


def test_get_status_includes_model_details(tmp_path):
    dev, transport, _ = running_device(tmp_path)
    readings = transport.invoke("get_status")[1]["readings"]
    assert readings["model"]["load_pct"] == 25.0 and readings["model"]["breaker_tripped"] is False
    assert readings["v"] == 218.5
