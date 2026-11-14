"""M6: local web control panel (solo version, no PIN)."""

import http.client
import json
import threading
import time

import pytest

from fakes import FakeTimers
from sim.config import SimConfig
from sim.controls import Controls
from sim.device import VirtualDevice
from sim.panel.server import HISTORY_POINTS, Panel, flt_text, serve
from sim.transport import DryRunTransport


def make_panel(tmp_path, fetch=None, api_url="https://api.example", **sensors):
    cfg = SimConfig(devices=["ems-sim-01"], state_dir=tmp_path, sensors={"noise": False, **sensors})
    stop = threading.Event()
    timers = FakeTimers()
    dev = VirtualDevice("ems-sim-01", cfg, DryRunTransport("ems-sim-01"), None, stop, interval=60,
                        timer_factory=timers)
    return Panel(Controls(dev), stop, api_url, fetch=fetch), dev, timers, stop


# ---- Panel (no HTTP) -------------------------------------------------------------------------

def test_live_snapshot_has_everything_the_page_needs(tmp_path):
    p, dev, timers, _ = make_panel(tmp_path)
    p.action("start")
    timers.fire()
    live = p.live()
    assert live["state"] == "RUNNING" and live["run"] == 2
    assert set(live["readings"]) == {"v", "i", "f", "fuel", "oil", "t", "vb", "rssi"}
    assert live["load_pct"] == 25.0 and live["speed"] == 1.0 and live["interval"] == 60
    assert live["limits"]["overtemp_c"] == 95.0 and live["limits"]["min_start_v"] == 11.0
    assert live["events"][-1]["text"].startswith("start: 200 accepted")


def test_fault_reason_and_plain_words(tmp_path):
    p, dev, timers, _ = make_panel(tmp_path, oil_start=2)
    p.action("start")
    live = p.live()
    assert live["state"] == "FAULT" and live["fault_reason"] == "Low Oil Level Warning"
    assert live["flt_text"] == ["Low oil (fault)"]
    assert flt_text(48) == ["Voltage out of range (alert)", "Frequency out of range (alert)"]


def test_warnings_in_plain_words(tmp_path):
    p, dev, *_ = make_panel(tmp_path, fuel_start_pct=8)
    dev.tick(1)
    assert p.live()["warnings"] == ["Low fuel warning"]


@pytest.mark.parametrize("action,value,check", [
    ("load", 80, lambda d: d.model.load == 0.8),
    ("fuel_empty", None, lambda d: d.model.fuel_pct == 0),
    ("refuel", None, lambda d: d.model.fuel_pct == 100),
    ("oil_cycle", None, lambda d: d.model.oil == 1),
    ("overheat", None, lambda d: d.model.overheat),
    ("weak_battery", None, lambda d: d.model.weak_battery),
    ("speed", 60, lambda d: d.speed == 60),
    ("speed", 99999, lambda d: d.speed == 3600),
])
def test_actions(tmp_path, action, value, check):
    p, dev, *_ = make_panel(tmp_path)
    assert p.action(action, value)["ok"]
    assert check(dev)


def test_unknown_action_rejected(tmp_path):
    p, *_ = make_panel(tmp_path)
    with pytest.raises(ValueError):
        p.action("explode")


def test_history_ring_buffer_and_since(tmp_path):
    p, *_ = make_panel(tmp_path)
    for _ in range(HISTORY_POINTS + 5):
        p.sample()
    assert len(p.history) == HISTORY_POINTS
    first = p.history[0]
    assert set(first) == {"ts", "run", "v", "f", "load", "fuel", "t", "vb"}
    assert p.history_points(since=p.history[-1]["ts"]) == []


def test_scenario_start_progress_and_stop(tmp_path):
    p, dev, _, stop = make_panel(tmp_path)
    res = p.start_scenario("normal-day")
    assert res["scenario"]["name"] == "normal-day" and dev.speed == 60
    with pytest.raises(ValueError, match="already running"):
        p.start_scenario("overload")
    time.sleep(0.3)
    assert p.live()["scenario"]["running"]
    p.stop_scenario()
    for _ in range(30):
        if not p.runner.running:
            break
        time.sleep(0.1)
    assert p.runner.result == "stopped"
    assert p.stop_scenario()["result"] == "no scenario running"
    stop.set()


def test_unknown_scenario_rejected(tmp_path):
    p, *_ = make_panel(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        p.start_scenario("nope")


def test_scenarios_list(tmp_path):
    p, *_ = make_panel(tmp_path)
    names = [s["name"] for s in p.scenarios()]
    assert names == ["normal-day", "out-of-fuel", "overheat", "overload"]


# ---- cloud view ----------------------------------------------------------------------------

def fake_api(state_ts_age=5, run=0, connection="Connected", twin_load=25.0, fail=False):
    calls = []

    def fetch(url):
        calls.append(url)
        if fail:
            raise OSError("connection refused")
        if url.endswith("/state"):
            return {"device_id": "ems-sim-01", "state": {"ts": int((time.time() - state_ts_age) * 1000), "run": run}}
        return {"connection_state": connection, "desired": {"$version": 2},
                "reported": {"settings": {"load_pct": twin_load}}}
    return fetch, calls


def test_cloud_view_agrees_and_is_cached(tmp_path):
    fetch, calls = fake_api()
    p, *_ = make_panel(tmp_path, fetch=fetch)
    c = p.cloud()
    assert c["configured"] and c["checks"] == []
    assert calls == ["https://api.example/api/devices/ems-sim-01/state",
                     "https://api.example/api/devices/ems-sim-01/twin"]
    p.cloud()
    assert len(calls) == 2            # cached for 10 s


def test_cloud_view_flags_differences(tmp_path):
    fetch, _ = fake_api(state_ts_age=600, run=2, connection="Disconnected", twin_load=60)
    p, *_ = make_panel(tmp_path, fetch=fetch)
    checks = " | ".join(p.cloud()["checks"])
    assert "600 s old" in checks and "run=2 differs" in checks
    assert "Disconnected" in checks and "load 60" in checks


def test_cloud_view_errors_and_off(tmp_path):
    fetch, _ = fake_api(fail=True)
    p, *_ = make_panel(tmp_path, fetch=fetch)
    c = p.cloud()
    assert "connection refused" in c["state"]["error"] and "error" in c["twin"]
    assert c["checks"] and "can't be fully compared" in c["checks"][0]    # never "agree" on errors
    p2, *_ = make_panel(tmp_path, api_url=None)
    assert p2.cloud() == {"configured": False}


# ---- HTTP ----------------------------------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    fetch, _ = fake_api()
    p, dev, timers, stop = make_panel(tmp_path, fetch=fetch)
    httpd = serve(p, "127.0.0.1", 0)
    port = httpd.server_address[1]

    def req(method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body) if not isinstance(body, bytes | str) else body
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        return r.status, dict(r.getheaders()), raw

    yield req, p, dev, timers
    stop.set()
    httpd.shutdown()


GOOD = {"Content-Type": "application/json", "X-EMS-Panel": "1"}


def test_page_and_assets_served_with_strict_headers(server):
    req, *_ = server
    for path, ctype in (("/", "text/html"), ("/app.js", "javascript"), ("/style.css", "text/css")):
        status, headers, body = req("GET", path)
        assert status == 200 and ctype in headers["Content-Type"] and body
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert headers["X-Frame-Options"] == "DENY"
    status, _, body = req("GET", "/")
    assert b"EMS-1000" in body and b"<script src=\"/app.js\"" in body


def test_json_endpoints(server):
    req, *_ = server
    for path, key in (("/api/live", "state"), ("/api/history", "points"), ("/api/scenarios", "scenarios"),
                      ("/api/cloud", "configured")):
        status, _, body = req("GET", path)
        assert status == 200 and key in json.loads(body)
    assert req("GET", "/nope")[0] == 404
    assert req("GET", "/../config/default.yaml")[0] == 404


def test_action_over_http(server):
    req, p, dev, timers = server
    status, _, body = req("POST", "/api/action", {"action": "start"}, GOOD)
    assert status == 200 and json.loads(body)["result"].startswith("200 accepted")
    timers.fire()
    assert dev.generator.state_name == "RUNNING"
    status, _, body = req("POST", "/api/action", {"action": "load", "value": 70}, GOOD)
    assert status == 200 and dev.model.load == 0.7


@pytest.mark.parametrize("headers,body,code", [
    ({"Content-Type": "application/json"}, {"action": "start"}, 403),              # no panel header
    ({"X-EMS-Panel": "1", "Content-Type": "text/plain"}, {"action": "start"}, 415),  # simple-request type
    ({"X-EMS-Panel": "1", "Content-Type": "application/x-www-form-urlencoded"}, "action=start", 415),
    (GOOD, "{not json", 400),
    (GOOD, [1, 2], 400),
    (GOOD, {"action": "explode"}, 400),
    (GOOD, "x" * 5000, 413),
])
def test_actions_protected_against_other_sites_and_bad_input(server, headers, body, code):
    req, p, dev, _ = server
    status, _, _ = req("POST", "/api/action", body, headers)
    assert status == code and dev.generator.state_name == "OFF"


def test_cors_preflight_never_approved(server):
    req, *_ = server
    status, headers, _ = req("OPTIONS", "/api/action", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-ems-panel,content-type"})
    assert status == 403 and "Access-Control-Allow-Origin" not in headers


def test_scenario_over_http(server):
    req, p, *_ = server
    status, _, body = req("POST", "/api/scenario/start", {"name": "overload"}, GOOD)
    assert status == 200 and json.loads(body)["scenario"]["name"] == "overload"
    assert req("POST", "/api/scenario/start", {"name": "overload"}, GOOD)[0] == 400  # already running
    assert req("POST", "/api/scenario/stop", {}, GOOD)[0] == 200
    assert req("POST", "/api/scenario/start", {"name": "nope"}, {**GOOD})[0] in (400,)
