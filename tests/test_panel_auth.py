"""Sharing the panel: PINs, sessions, roles and login limits."""

import http.client
import json
import threading

import pytest

from fakes import FakeTimers
from sim.config import SimConfig
from sim.controls import Controls
from sim.device import VirtualDevice
from sim.panel.auth import COOKIE, MAX_FAILURES, OPERATOR, VIEWER, Access, AccessError, cookie_value
from sim.panel.server import Panel, serve
from sim.transport import DryRunTransport

V_PIN, O_PIN = "246810", "135799"
JSON = {"Content-Type": "application/json", "X-EMS-Panel": "1"}


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---- Access (no HTTP) -----------------------------------------------------------------------

def test_solo_when_no_pins():
    assert not Access().sharing


@pytest.mark.parametrize("viewer,operator", [(V_PIN, None), (None, O_PIN), ("123", O_PIN), (V_PIN, V_PIN)])
def test_bad_configurations_refused(viewer, operator):
    with pytest.raises(AccessError):
        Access(viewer, operator)


def test_pins_give_roles():
    a = Access(V_PIN, O_PIN)
    assert a.sharing
    assert a.login(V_PIN, "c") == VIEWER
    assert a.login(O_PIN, "c") == OPERATOR
    assert a.login("000000", "c") is None


def test_session_round_trip_and_tampering():
    a = Access(V_PIN, O_PIN)
    token = a.issue(VIEWER)
    assert a.role_of(token) == VIEWER
    forged = token.replace("viewer.", "operator.", 1)
    assert a.role_of(forged) is None
    assert a.role_of("operator.9999999999.x.bad") is None
    assert a.role_of(None) is None and a.role_of("junk") is None


def test_session_expires():
    clock = Clock()
    a = Access(V_PIN, O_PIN, clock=clock, session_s=60)
    token = a.issue(OPERATOR)
    clock.t += 59
    assert a.role_of(token) == OPERATOR
    clock.t += 2
    assert a.role_of(token) is None


def test_sessions_survive_restart_but_not_a_pin_change():
    token = Access(V_PIN, O_PIN).issue(OPERATOR)
    assert Access(V_PIN, O_PIN).role_of(token) == OPERATOR         # same PINs: still valid
    assert Access(V_PIN, "999999").role_of(token) is None          # changed PIN: signed out


def test_wrong_pins_lock_the_client_out():
    clock = Clock()
    a = Access(V_PIN, O_PIN, clock=clock)
    for _ in range(MAX_FAILURES):
        assert a.locked_for("1.2.3.4") == 0
        a.login("000000", "1.2.3.4")
    assert a.locked_for("1.2.3.4") > 0
    assert a.locked_for("5.6.7.8") == 0       # other visitors unaffected
    clock.t += 301
    assert a.locked_for("1.2.3.4") == 0


def test_cookie_parsing():
    assert cookie_value("a=1; ems_panel=tok.en; b=2") == "tok.en"
    assert cookie_value(None) is None and cookie_value("a=1") is None


# ---- HTTP ------------------------------------------------------------------------------------

@pytest.fixture
def shared(tmp_path):
    cfg = SimConfig(devices=["ems-sim-01"], state_dir=tmp_path, sensors={"noise": False})
    stop = threading.Event()
    timers = FakeTimers()
    dev = VirtualDevice("ems-sim-01", cfg, DryRunTransport("ems-sim-01"), None, stop, interval=60,
                        timer_factory=timers)
    panel = Panel(Controls(dev), stop, None)
    httpd = serve(panel, "127.0.0.1", 0, Access(V_PIN, O_PIN))
    port = httpd.server_address[1]

    def req(method, path, body=None, headers=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = f"{COOKIE}={cookie}"
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        return r.status, dict(r.getheaders()), raw

    def login(pin, extra=None):
        status, headers, body = req("POST", "/api/login", {"pin": pin}, {**JSON, **(extra or {})})
        token = cookie_value(headers.get("Set-Cookie", "").split(";")[0])
        return status, headers, json.loads(body), token

    yield req, login, panel, dev, timers
    stop.set()
    httpd.shutdown()


def test_pages_need_a_session(shared):
    req, *_ = shared
    status, headers, _ = req("GET", "/")
    assert status == 302 and headers["Location"] == "/login"
    assert req("GET", "/app.js")[0] == 302
    for path in ("/login", "/login.js", "/style.css"):
        assert req("GET", path)[0] == 200
    status, _, body = req("GET", "/api/live")
    assert status == 401 and json.loads(body)["login"] == "/login"


def test_viewer_reads_but_cannot_act(shared):
    req, login, panel, dev, _ = shared
    status, headers, data, token = login(V_PIN)
    assert status == 200 and data["role"] == "viewer"
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    assert req("GET", "/", cookie=token)[0] == 200
    assert json.loads(req("GET", "/api/session", cookie=token)[2]) == {"sharing": True, "role": "viewer"}
    assert req("GET", "/api/live", cookie=token)[0] == 200
    status, _, body = req("POST", "/api/action", {"action": "start"}, JSON, cookie=token)
    assert status == 403 and "View only" in json.loads(body)["error"]
    assert req("POST", "/api/scenario/start", {"name": "overload"}, JSON, cookie=token)[0] == 403
    assert dev.generator.state_name == "OFF"


def test_operator_can_act_and_is_named_in_the_log(shared):
    req, login, panel, dev, timers = shared
    *_, token = login(O_PIN)
    status, _, body = req("POST", "/api/action", {"action": "start"}, JSON, cookie=token)
    assert status == 200 and json.loads(body)["result"].startswith("200 accepted")
    assert any(e["text"].startswith("[operator] start") for e in panel.events)
    assert any(e["text"] == "[operator] signed in" for e in panel.events)


def test_wrong_pin_then_lockout(shared):
    req, login, *_ = shared
    for _ in range(MAX_FAILURES):
        status, _, data, token = login("000000")
        assert status == 401 and data["error"] == "Wrong PIN" and token is None
    status, headers, data, _ = login(O_PIN)          # right PIN, but this client is locked
    assert status == 429 and "Retry-After" in headers


def test_secure_cookie_behind_https_proxy_and_logout(shared):
    req, login, *_ = shared
    _, headers, _, token = login(O_PIN, {"X-Forwarded-Proto": "https"})
    assert "Secure" in headers["Set-Cookie"]
    status, headers, _ = req("POST", "/api/logout", {}, JSON, cookie=token)
    assert status == 200 and "Max-Age=0" in headers["Set-Cookie"]


def test_forged_cookie_rejected(shared):
    req, *_ = shared
    assert req("GET", "/api/live", cookie="operator.9999999999.abc.forged")[0] == 401


def test_login_still_needs_the_panel_header(shared):
    req, *_ = shared
    assert req("POST", "/api/login", {"pin": O_PIN}, {"Content-Type": "application/json"})[0] == 403
