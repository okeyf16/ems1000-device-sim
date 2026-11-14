import json
import threading

import pytest

from sim.budget import BudgetGuard
from sim.commands import CommandHandler
from sim.config import SimConfig
from sim.device import VirtualDevice
from sim.state_machine import FAULT_LOW_OIL, Generator, RunState
from sim.transport import DryRunTransport

from fakes import FakeTimers


def make_gen(safe=(True, "", 0)):
    timers, changes = FakeTimers(), []
    check = {"result": safe}
    gen = Generator(lambda: check["result"], on_change=lambda o, n: changes.append((o, n)),
                    timer_factory=timers)
    return gen, timers, changes, check


# ---- state machine --------------------------------------------------------------

def test_start_cranks_then_runs_with_immediate_reply():
    gen, timers, changes, _ = make_gen()
    status, body = gen.start()
    assert (status, body["result"], body["state"]) == (200, "accepted", "CRANKING")
    assert gen.state == RunState.CRANK            # reply came back before the crank finished
    assert timers.fire() == 2.5
    assert gen.state == RunState.RUN
    assert changes == [(RunState.OFF, RunState.CRANK), (RunState.CRANK, RunState.RUN)]


def test_start_refused_when_running_or_cranking():
    gen, timers, _, _ = make_gen()
    gen.start()
    assert gen.start()[0] == 400                 # while cranking
    timers.fire()
    status, body = gen.start()                    # while running
    assert status == 400 and body["reason"] == "Generator is already running"


def test_safety_check_failure_locks_out_with_409():
    gen, _, changes, _ = make_gen(safe=(False, "Low Oil Level Warning", FAULT_LOW_OIL))
    status, body = gen.start()
    assert (status, body["result"], body["state"], body["reason"]) == (409, "refused", "FAULT", "Low Oil Level Warning")
    assert gen.flt == FAULT_LOW_OIL
    assert changes == [(RunState.OFF, RunState.FAULT)]
    assert gen.start()[0] == 409                 # still locked out


def test_stop_from_run_goes_through_stopping_to_off():
    gen, timers, _, _ = make_gen()
    gen.start(); timers.fire()
    status, body = gen.stop()
    assert (status, body["state"]) == (200, "STOPPING")
    assert timers.fire() == 3.0
    assert gen.state == RunState.OFF


def test_stop_while_cranking_cancels_crank():
    gen, timers, _, _ = make_gen()
    gen.start()
    gen.stop()
    timers.fire()                                 # the stop timer; the crank timer was cancelled
    assert gen.state == RunState.OFF
    assert all(t.cancelled for t in timers.pending)


def test_stop_when_off_is_harmless():
    gen, _, changes, _ = make_gen()
    status, body = gen.stop()
    assert (status, body["result"], body["note"]) == (200, "accepted", "already off")
    assert changes == []


def test_reset_fault_refused_until_condition_clears():
    gen, _, _, check = make_gen(safe=(False, "Fuel Level Below Threshold", 2))
    gen.start()
    status, body = gen.reset_fault()
    assert status == 409 and "Fuel Level Below Threshold" in body["reason"]
    check["result"] = (True, "", 0)              # refuelled
    status, body = gen.reset_fault()
    assert (status, body["result"], body["state"]) == (200, "cleared", "OFF")
    assert gen.flt == 0
    assert gen.start()[0] == 200


def test_reset_fault_without_fault():
    gen, _, _, _ = make_gen()
    assert gen.reset_fault()[1]["result"] == "no_fault"


def test_trip_while_running_kills_engine():
    gen, timers, _, _ = make_gen()
    gen.start(); timers.fire()
    gen.trip(4, "Over-temperature")
    assert gen.state == RunState.FAULT and gen.flt == 4


# ---- command handler --------------------------------------------------------------

@pytest.mark.parametrize("name", ["remote_start", "start"])
def test_start_aliases(name):
    gen, _, _, _ = make_gen()
    status, body = CommandHandler(gen, snapshot=dict).handle(name, {"command_id": "ab12"}, "rid-1")
    assert status == 200 and body["cmd_id"] == "ab12"


def test_cmd_id_falls_back_to_request_id_and_unknown_is_404():
    gen, _, _, _ = make_gen()
    h = CommandHandler(gen, snapshot=dict)
    assert h.handle("remote_stop", None, "rid-9")[1]["cmd_id"] == "rid-9"
    status, body = h.handle("self_destruct", {}, "rid-10")
    assert status == 404 and body["result"] == "unknown_method" and "reset_fault" in body["supported"]


def test_get_status_returns_state_and_readings():
    gen, _, _, _ = make_gen()
    status, body = CommandHandler(gen, snapshot=lambda: {"v": 0.0}).handle("get_status", {}, "r")
    assert status == 200 and body["state"] == "OFF" and body["run"] == 0 and body["readings"] == {"v": 0.0}


# ---- device wiring --------------------------------------------------------------

def _device(tmp_path, budget=None):
    cfg = SimConfig(devices=["ems-sim-01"], state_dir=tmp_path, sensors={"noise": False})
    transport, timers = DryRunTransport("ems-sim-01"), FakeTimers()
    dev = VirtualDevice("ems-sim-01", cfg, transport, budget, threading.Event(),
                        interval=0, timer_factory=timers)
    return dev, transport, timers


def test_commands_over_transport_send_on_change_telemetry(tmp_path):
    dev, transport, timers = _device(tmp_path)
    status, body = transport.invoke("remote_start", {"command_id": "c1"})
    assert status == 200
    timers.fire()
    runs = [json.loads(b)["run"] for b, _ in transport.sent]
    assert runs == [1, 2]                         # crank, run: one message per change
    running = json.loads(transport.sent[-1][0])
    # ECO4990ES at the default 25 % load: 220 V - 6 x 0.25, governor 51.5 - 1.5 x 0.25
    assert running["v"] == 218.5 and running["f"] == 51.12 and running["i"] == 3.41
    cranking = json.loads(transport.sent[0][0])
    assert cranking["vb"] == 10.6 and cranking["v"] == 0.0
    transport.invoke("stop"); timers.fire()
    assert [json.loads(b)["run"] for b, _ in transport.sent][-2:] == [3, 0]


def test_low_oil_start_reports_fault_in_telemetry(tmp_path):
    dev, transport, _ = _device(tmp_path)
    dev.sensors.oil = 2
    status, body = transport.invoke("start")
    assert status == 409 and body["reason"] == "Low Oil Level Warning"
    last = json.loads(transport.sent[-1][0])
    assert last["run"] == 4 and last["flt"] & FAULT_LOW_OIL


def test_commands_count_against_budget(tmp_path):
    budget = BudgetGuard(tmp_path, stop_at=7500, daily_limit=8000)
    dev, transport, timers = _device(tmp_path, budget)
    transport.invoke("get_status")                # 1 command, no telemetry
    transport.invoke("start"); timers.fire()      # 1 command + 2 state messages
    assert budget.used == 4
