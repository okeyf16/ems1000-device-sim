"""Sharing the panel: named PINs, roles, sessions, who's signed in, login limits."""

import http.client
import json
import threading

import pytest

import cli
from fakes import FakeTimers
from sim.config import SimConfig
from sim.controls import Controls
from sim.device import VirtualDevice
from sim.panel.auth import (
    ACTIVE_S,
    COOKIE,
    MAX_FAILURES,
    OPERATOR,
    VIEWER,
    Access,
    AccessError,
    cookie_value,
    legacy_users,
    parse_users,
)
from sim.panel.server import Panel, serve
from sim.transport import DryRunTransport

USERS = "Okey:operator:135799, Paul:operator:777111, Emeka:viewer:246810"
JSON = {"Content-Type": "application/json", "X-EMS-Panel": "1"}


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def access(text=USERS, **kw):
    return Access(parse_users(text), **kw)


# ---- parsing ----------------------------------------------------------------------------------

def test_users_parsed_with_names_and_roles():
    users = parse_users(" Okey:operator:135799 ,Emeka:Viewer:246810, ")
    assert [(u.name, u.role) for u in users] == [("Okey", OPERATOR), ("Emeka", VIEWER)]


@pytest.mark.parametrize("text,hint", [
    ("Okey:135799", "Name:role:PIN"),
    ("Okey:admin:135799", "viewer or operator"),
    ("Okey:operator:123", "at least 6"),
    ("Okey:operator:135799, okey:viewer:246810", "more than once"),
    ("Okey:operator:135799, Paul:viewer:135799", "shares a PIN"),
    ("<script>:operator:135799", "name must be"),
])
def test_bad_entries_refused_without_showing_pins(text, hint):
    with pytest.raises(AccessError) as err:
        parse_users(text)
    assert hint in str(err.value) and "135799" not in str(err.value)


def test_legacy_two_pins_still_work():
    users = legacy_users("246810", "135799")
    assert [(u.name, u.role) for u in users] == [("Viewer", VIEWER), ("Operator", OPERATOR)]
    with pytest.raises(AccessError):
        legacy_users("246810", None)
    assert legacy_users(None, None) == []


def test_named_users_take_precedence_over_legacy_pins():
    a = Access.from_env({"EMS_PANEL_USERS": USERS,
                         "EMS_PANEL_VIEWER_PIN": "111111", "EMS_PANEL_OPERATOR_PIN": "222222"})
    assert [u["name"] for u in a.roster()] == ["Okey", "Paul", "Emeka"]
    assert not Access.from_env({}).sharing


# ---- sessions ----------------------------------------------------------------------------------

def test_pin_identifies_the_person():
    a = access()
    assert a.login("777111", "c").name == "Paul"
    assert a.login("246810", "c").role == VIEWER
    assert a.login("000000", "c") is None


def test_session_round_trip_and_tampering():
    a = access()
    token = a.issue(a.login("246810", "c"))
    assert a.session_of(token).name == "Emeka"
    parts = token.split(".")
    parts[1] = "operator"                        # try to promote the viewer
    assert a.session_of(".".join(parts)) is None
    assert a.session_of("junk") is None and a.session_of(None) is None


def test_session_expires():
    clock = Clock()
    a = access(clock=clock, session_s=60)
    token = a.issue(a.login("135799", "c"))
    clock.t += 59
    assert a.session_of(token).name == "Okey"
    clock.t += 2
    assert a.session_of(token) is None


def test_removing_or_changing_one_person_signs_out_only_them():
    a = access()
    paul = a.issue(a.login("777111", "c"))
    okey = a.issue(a.login("135799", "c"))
    without_paul = access("Okey:operator:135799, Emeka:viewer:246810")
    assert without_paul.session_of(paul) is None
    new_pin = access("Okey:operator:135799, Paul:operator:999000, Emeka:viewer:246810")
    assert new_pin.session_of(paul) is None
    assert access().session_of(okey).name == "Okey"            # restart, same entries: still in


def test_signed_in_list_and_sign_out():
    clock = Clock()
    a = access(clock=clock)
    paul = a.issue(a.login("777111", "c"))
    clock.t += 30
    a.login("246810", "d")
    names = [p["name"] for p in a.signed_in()]
    assert names == ["Emeka", "Paul"]                          # most recent first
    assert a.signed_in()[1] == {"name": "Paul", "role": OPERATOR, "idle_s": 30}
    a.sign_out(paul)
    assert [p["name"] for p in a.signed_in()] == ["Emeka"]
    clock.t += ACTIVE_S + 1
    assert a.signed_in() == []


def test_wrong_pins_lock_the_client_out():
    clock = Clock()
    a = access(clock=clock)
    for _ in range(MAX_FAILURES):
        assert a.locked_for("1.2.3.4") == 0
        a.login("000000", "1.2.3.4")
    assert a.locked_for("1.2.3.4") > 0 and a.locked_for("5.6.7.8") == 0
    clock.t += 301
    assert a.locked_for("1.2.3.4") == 0


def test_cookie_parsing():
    assert cookie_value("a=1; ems_panel=tok.en; b=2") == "tok.en"
    assert cookie_value(None) is None and cookie_value("a=1") is None


def test_cli_lists_panel_users_without_pins(monkeypatch, capsys):
    monkeypatch.setenv("EMS_PANEL_USERS", USERS)
    assert cli.main(["panel-users"]) == 0
    out = capsys.readouterr().out
    assert "Paul" in out and "operator" in out and "777111" not in out
    monkeypatch.setenv("EMS_PANEL_USERS", "Paul:boss:777111")
    assert cli.main(["panel-users"]) == 2


# ---- HTTP --------------------------------------------------------------------------------------

@pytest.fixture
def shared(tmp_path):
    cfg = SimConfig(devices=["ems-sim-01"], state_dir=tmp_path, sensors={"noise": False})
    stop = threading.Event()
    dev = VirtualDevice("ems-sim-01", cfg, DryRunTransport("ems-sim-01"), None, stop, interval=60,
                        timer_factory=FakeTimers())
    panel = Panel(Controls(dev), stop, None)
    httpd = serve(panel, "127.0.0.1", 0, access())
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

    yield req, login, panel, dev
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


def test_viewer_sees_who_is_signed_in_but_cannot_act(shared):
    req, login, panel, dev = shared
    login("777111")                                            # Paul signs in elsewhere
    status, headers, data, token = login("246810")
    assert status == 200 and data == {"ok": True, "role": "viewer", "name": "Emeka"}
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    session = json.loads(req("GET", "/api/session", cookie=token)[2])
    assert session["name"] == "Emeka" and session["role"] == "viewer"
    assert {p["name"] for p in session["signed_in"]} == {"Emeka", "Paul"}
    status, _, body = req("POST", "/api/action", {"action": "start"}, JSON, cookie=token)
    assert status == 403 and "viewer PIN" in json.loads(body)["error"]
    assert dev.generator.state_name == "OFF"


def test_operator_actions_are_logged_by_name(shared):
    req, login, panel, dev = shared
    *_, token = login("777111")
    status, _, body = req("POST", "/api/action", {"action": "start"}, JSON, cookie=token)
    assert status == 200 and json.loads(body)["result"].startswith("200 accepted")
    texts = [e["text"] for e in panel.events]
    assert "[Paul] signed in (operator)" in texts
    assert any(t.startswith("[Paul] start") for t in texts)


def test_logout_logs_and_leaves_the_list(shared):
    req, login, panel, _ = shared
    *_, paul = login("777111")
    *_, emeka = login("246810")
    status, headers, _ = req("POST", "/api/logout", {}, JSON, cookie=paul)
    assert status == 200 and "Max-Age=0" in headers["Set-Cookie"]
    assert "[Paul] signed out" in [e["text"] for e in panel.events]
    session = json.loads(req("GET", "/api/session", cookie=emeka)[2])
    assert [p["name"] for p in session["signed_in"]] == ["Emeka"]


def test_wrong_pin_then_lockout(shared):
    req, login, *_ = shared
    for _ in range(MAX_FAILURES):
        status, _, data, token = login("000000")
        assert status == 401 and data["error"] == "Wrong PIN" and token is None
    status, headers, *_ = login("777111")
    assert status == 429 and "Retry-After" in headers


def test_secure_cookie_behind_https_proxy(shared):
    _, login, *_ = shared
    _, headers, _, _ = login("135799", {"X-Forwarded-Proto": "https"})
    assert "Secure" in headers["Set-Cookie"]


def test_forged_cookie_rejected(shared):
    req, *_ = shared
    assert req("GET", "/api/live", cookie="UGF1bA.operator.9999999999.ffff.abc.forged")[0] == 401


def test_login_still_needs_the_panel_header(shared):
    req, *_ = shared
    assert req("POST", "/api/login", {"pin": "777111"}, {"Content-Type": "application/json"})[0] == 403
