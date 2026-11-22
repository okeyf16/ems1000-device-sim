"""Local web control panel (M6): `python cli.py run --panel`.

Python's built-in HTTP server, JSON endpoints and three static files. No IoT Hub
messages are used by the panel itself; its buttons go through the same local
controls as the keyboard shortcuts.

Access: solo by default (127.0.0.1, reached through the Codespace's private forwarded
port). Define people in EMS_PANEL_USERS (Name:role:PIN) to share it: then every page
and API call needs a session from the login page (see auth.py), viewers are read-only,
only operators can use the controls, and everyone sees who is signed in. Every write also needs the `X-EMS-Panel`
header and a JSON body: browsers won't let another website send those without a CORS
preflight, which this server never approves, so other sites can't trigger actions.
"""

from __future__ import annotations

import collections
import functools
import json
import logging
import mimetypes
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..controls import ACTION_NAMES, Controls
from ..physics import ALERT_FREQUENCY, ALERT_VOLTAGE, FAULT_LOW_BATTERY, FAULT_LOW_FUEL, FAULT_LOW_OIL, FAULT_OVER_TEMP
from ..scenario import ScenarioRunner, available, load_scenario, start_scenario
from .auth import COOKIE, OPERATOR, SOLO, Access, User, cookie_value

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css",
                "/login": "login.html", "/login.js": "login.js"}
PUBLIC_FILES = {"/login", "/login.js", "/style.css"}   # reachable without a session when sharing
ACTION_HEADER = "X-EMS-Panel"
MAX_BODY = 4096
HISTORY_POINTS = 900          # 30 min at one point every 2 s
HISTORY_EVERY_S = 2.0
# Cloud view refresh: the stored state and alerts are table reads (free); twin reads count
# against the IoT Hub's 8,000 daily messages, so the twin is read far less often.
CLOUD_TTL_S = {"state": 10.0, "alerts": 30.0, "system_alerts": 30.0, "twin": 120.0, "api_access": 60.0}

# 12 V lead-acid state of charge from resting voltage (estimate); same curve as the API.
_SOC_POINTS = [(11.8, 0), (12.0, 25), (12.2, 50), (12.4, 75), (12.5, 90), (12.7, 100)]


def estimate_soc(vb: float) -> int:
    if vb <= _SOC_POINTS[0][0]:
        return 0
    if vb >= _SOC_POINTS[-1][0]:
        return 100
    for (v1, s1), (v2, s2) in zip(_SOC_POINTS, _SOC_POINTS[1:], strict=False):
        if v1 <= vb <= v2:
            return round(s1 + (s2 - s1) * (vb - v1) / (v2 - v1))
    return 0

FLT_TEXT = [
    (FAULT_LOW_OIL, "Low oil (fault)"),
    (FAULT_LOW_FUEL, "Low / no fuel (fault)"),
    (FAULT_OVER_TEMP, "Over-temperature (fault)"),
    (FAULT_LOW_BATTERY, "Starter battery too low (fault)"),
    (ALERT_VOLTAGE, "Voltage out of range (alert)"),
    (ALERT_FREQUENCY, "Frequency out of range (alert)"),
]
WARNING_TEXT = {"low_fuel": "Low fuel warning", "low_oil": "Low oil warning"}


def flt_text(flt: int) -> list[str]:
    return [text for bit, text in FLT_TEXT if flt & bit]


class Panel:
    """State and actions behind the web page. Also usable without HTTP (tests)."""

    def __init__(self, controls: Controls, stop_event: threading.Event, api_url: str | None = None,
                 runner: ScenarioRunner | None = None, fetch=None, api_key: str | None = None):
        self.controls = controls
        self.device = controls.device
        self.stop_event = stop_event
        self.api_url = (api_url or "").rstrip("/") or None
        self.runner = runner
        self.history: collections.deque = collections.deque(maxlen=HISTORY_POINTS)
        self.events: collections.deque = collections.deque(maxlen=50)   # action log shown on the page
        self.api_key = api_key or None
        self._fetch = fetch or functools.partial(_http_get_json, api_key=self.api_key)
        self._cloud_parts: dict[str, tuple[float, dict]] = {}
        self._rest_soc: int | None = None   # battery charge last measured with the engine off
        self._lock = threading.Lock()

    # ---- read ------------------------------------------------------------------------
    def live(self) -> dict:
        d, m = self.device, self.device.model
        run = int(d.generator.state)
        r = m.read(run)
        flt = d.flt
        extras = m.extras()
        return {
            "device_id": d.device_id,
            "state": d.generator.state_name, "run": run, "flt": flt,
            "flt_text": flt_text(flt),
            "fault_reason": d.generator.fault_reason if d.generator.state_name == "FAULT" else "",
            "warnings": [WARNING_TEXT.get(w, w) for w in sorted(m.warnings)],
            "breaker_tripped": extras["breaker_tripped"],
            "readings": {"v": r.v, "i": r.i, "f": r.f, "fuel": r.fuel, "oil": r.oil, "t": r.t,
                         "vb": r.vb, "rssi": r.rssi,
                         # load as measured (current vs rated), like the API; the slider's
                         # setting is the top-level load_pct
                         "load_pct": round(r.i / m.rated_a * 100, 1) if m.rated_a else 0.0,
                         "soc_pct": estimate_soc(r.vb)},
            "soc_reliable": run in (0, 4),   # cranking/running skews battery voltage
            "load_pct": extras["load_pct"], "fuel_l": extras["fuel_l"], "fuel_l_per_h": extras["fuel_l_per_h"],
            "overheat": m.overheat, "weak_battery": m.weak_battery,
            "speed": d.speed, "interval": d.interval, "seq": d.seq, "boot": d.boot,
            "limits": {
                "nominal_v": m.nominal_v, "v_tolerance_pct": m.v_tolerance * 100,
                "f_low": m.f_low, "f_high": m.f_high, "rated_a": round(m.rated_a, 2),
                "max_a": round(m.rated_a * m.max_load, 2), "low_fuel_warn_pct": m.low_fuel_warn_pct,
                "low_fuel_interlock_pct": m.low_fuel_interlock_pct, "restart_temp_c": m.restart_temp_c,
                "overtemp_c": m.overtemp_c, "min_start_v": m.min_start_v,
            },
            "scenario": self.runner.progress() if self.runner else None,
            "events": list(self.events)[-12:],
        }

    def sample(self) -> None:
        d, m = self.device, self.device.model
        run = int(d.generator.state)
        r = m.read(run)
        if run in (0, 4) or self._rest_soc is None:   # engine off: voltage tells the charge
            self._rest_soc = estimate_soc(r.vb)
        self.history.append({"ts": int(time.time() * 1000), "run": run, "v": r.v, "f": r.f,
                             "load": round(m.load * 100, 1), "fuel": r.fuel, "t": r.t, "vb": r.vb,
                             "soc": self._rest_soc})

    def history_points(self, since: int = 0) -> list[dict]:
        return [p for p in list(self.history) if p["ts"] > since]

    def scenarios(self) -> list[dict]:
        out = []
        for name in available():
            sc = load_scenario(name)
            out.append({"name": name, "description": sc.description, "speed": sc.speed,
                        "steps": len(sc.steps)})
        return out

    def _cloud_part(self, key: str, path: str, now: float) -> dict:
        with self._lock:
            hit = self._cloud_parts.get(key)
            if hit and now - hit[0] < CLOUD_TTL_S[key]:
                return hit[1]
        try:
            value = self._fetch(self.api_url + path)
        except Exception as err:  # noqa: BLE001 - shown on the page
            value = {"error": str(getattr(err, "reason", None) or err)}
        if isinstance(value, dict) and value.get("status") == 401:
            value = {"error": "API key missing or wrong: set the EMS_API_KEY secret", "status": 401}
        with self._lock:
            self._cloud_parts[key] = (now, value)
        return value

    def cloud(self) -> dict:
        """The staging API's view: stored /state (10 s), device and system alerts (30 s) and the twin (120 s)."""
        if not self.api_url:
            return {"configured": False}
        now = time.monotonic()
        dev = self.device.device_id
        out: dict = {"configured": True, "api_url": self.api_url, "fetched_at": int(time.time() * 1000),
                     "key_set": bool(self.api_key)}
        out["state"] = self._cloud_part("state", f"/api/devices/{dev}/state", now)
        out["twin"] = self._cloud_part("twin", f"/api/devices/{dev}/twin", now)
        out["alerts"] = self._cloud_part("alerts", f"/api/alerts?device_id={dev}&limit=20", now)
        # Pipeline-wide alerts (e.g. slow cloud processing) have no device; the API files them under _system.
        out["system_alerts"] = self._cloud_part("system_alerts", "/api/alerts?device_id=_system&limit=10", now)
        # Who used the staging API today (people signed in with Microsoft, and API keys)
        out["api_access"] = self._cloud_part("api_access", "/api/access?days=1", now)
        failed = [k for k in ("state", "twin") if isinstance(out.get(k), dict) and "error" in out[k]]
        out["checks"] = ([f"Couldn't read {' and '.join(failed)} from the staging API, so device and cloud "
                          "can't be fully compared"] if failed else []) + self._compare(out)
        return out

    def _compare(self, cloud: dict) -> list[str]:
        """Plain-words differences between the device and the cloud."""
        notes = []
        resp = cloud.get("state") or {}
        if isinstance(resp.get("status"), dict):          # API since PR #14: readings/status/link
            ts = (resp.get("link") or {}).get("ts")
            run = resp["status"].get("run")
        elif isinstance(resp.get("state"), dict):         # older API: {"device_id", "state": {...}}
            ts, run = resp["state"].get("ts"), resp["state"].get("run")
        else:
            ts = run = None
        if isinstance(ts, int | float):
            age = time.time() - ts / 1000
            if age > 2 * max(self.device.interval, 1) + 15:
                notes.append(f"Cloud's last message is {age:.0f} s old (heartbeat every "
                             f"{self.device.interval} s): telemetry may not be arriving")
        if run is not None and run != int(self.device.generator.state):
            notes.append(f"Cloud state run={run} differs from device run="
                         f"{int(self.device.generator.state)} (normal for a few seconds after a change)")
        link = resp.get("link") if isinstance(resp.get("link"), dict) else {}
        if link.get("connection_status") == "Offline":
            since = _parse_iso(link.get("last_status_change"))
            received = _parse_iso(resp.get("received_at"))
            # Offline, yet a message arrived after that: a Connected event was missed. Allow
            # a minute and a half for Event Grid and the cloud's own correction.
            if since is not None and received is not None and received > since and time.time() - since > 90:
                notes.append("Cloud shows the device Offline although messages arrived after that "
                             "(a Connected event was missed; the cloud corrects it within a minute)")
        twin = cloud.get("twin") or {}
        if "connection_state" in twin and twin["connection_state"] != "Connected":
            notes.append(f"IoT Hub reports the device as {twin['connection_state']}")
        rep = (twin.get("reported") or {}).get("settings") or {}
        load = rep.get("load_pct")
        if isinstance(load, int | float) and abs(load - self.device.model.load * 100) > 0.5:
            notes.append(f"Twin reports load {load:g} %, device is at {self.device.model.load * 100:g} % "
                         "(load changes from keys/panel aren't reported until the next settings change)")
        return notes

    # ---- write -----------------------------------------------------------------------
    def _log(self, text: str) -> None:
        self.events.append({"ts": int(time.time() * 1000), "text": text})
        log.info("[panel] %s", text)

    def action(self, name: str, value=None, actor: str | None = None) -> dict:
        if name not in ACTION_NAMES:
            raise ValueError(f"unknown action {name!r}")
        result = self.controls.do(name, value)
        label = name + (f" {value}" if value is not None else "")
        self._log(f"{_by(actor)}{label}: {result}")
        return {"ok": True, "action": name, "result": result}

    def start_scenario(self, name: str, actor: str | None = None) -> dict:
        if self.runner and self.runner.running:
            raise ValueError(f"scenario '{self.runner.scenario.name}' is already running; stop it first")
        sc = load_scenario(name)        # ValueError if unknown
        if sc.speed is not None:
            self.device.speed = sc.speed
        self.runner = ScenarioRunner(sc, self.controls, self.stop_event)
        start_scenario(self.runner)
        self._log(f"{_by(actor)}scenario {name} started (speed {self.device.speed:g}x)")
        return {"ok": True, "scenario": self.runner.progress()}

    def stop_scenario(self, actor: str | None = None) -> dict:
        if not self.runner or not self.runner.running:
            return {"ok": True, "result": "no scenario running"}
        self.runner.cancel()
        self._log(f"{_by(actor)}scenario {self.runner.scenario.name} stop requested")
        return {"ok": True, "result": "stopping"}

    # ---- background ------------------------------------------------------------------
    def start_sampler(self) -> threading.Thread:
        def loop():
            while not self.stop_event.wait(HISTORY_EVERY_S):
                try:
                    self.sample()
                except Exception:  # noqa: BLE001
                    log.exception("panel history sample failed")
        self.sample()
        t = threading.Thread(target=loop, name="panel-history", daemon=True)
        t.start()
        return t


def _by(actor: str | None) -> str:
    """'[operator] ' prefix for the action log when the panel is shared."""
    return f"[{actor}] " if actor else ""


def _parse_iso(value) -> float | None:
    """ISO 8601 with offset (the API's WAT times) -> epoch seconds; None if unreadable."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _http_get_json(url: str, api_key: str | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310 - URL from config
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return {"error": f"HTTP {err.code}", "status": err.code}


# ---- HTTP ---------------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    panel: Panel     # set on the subclass made in serve()
    access: Access   # sharing settings (solo when no PINs)
    server_version = "EMSPanel/1"

    def log_message(self, fmt, *args):  # quiet; actions are logged by Panel
        log.debug("panel http: " + fmt, *args)

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data, extra: dict | None = None) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json", extra)

    def _redirect(self, where: str) -> None:
        self._send(302, b"", "text/plain", {"Location": where})

    # ---- sessions ----------------------------------------------------------------------
    def _user(self) -> User | None:
        """'you' (operator) in solo mode; otherwise the signed-in person, or None."""
        if not self.access.sharing:
            return SOLO
        return self.access.session_of(cookie_value(self.headers.get("Cookie")))

    def _client(self) -> str:
        # Codespaces forwards through a proxy that appends the visitor's address to
        # X-Forwarded-For; take the last entry (earlier ones could be set by the visitor)
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
        return forwarded or self.client_address[0]

    def _cookie(self, value: str, max_age: int) -> dict:
        secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
        return {"Set-Cookie": f"{COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict{secure}"}

    def _actor(self, user: User) -> str | None:
        return user.name if self.access.sharing else None

    # ---- GET ----------------------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        user = self._user()
        role = user.role if user else None
        if path == "/login" and (not self.access.sharing or role):
            return self._redirect("/")
        if path in STATIC_FILES:
            if role is None and path not in PUBLIC_FILES:
                return self._redirect("/login")
            f = STATIC / STATIC_FILES[path]
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if f.suffix == ".html":
                ctype = "text/html"
            return self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") or
                                                             ctype.endswith("javascript") else ""))
        if path.startswith("/api/") and role is None:
            return self._json(401, {"error": "login required", "login": "/login"})
        p = self.panel
        try:
            if path == "/api/session":
                return self._json(200, {"sharing": self.access.sharing, "role": role,
                                        "name": user.name if self.access.sharing else None,
                                        "signed_in": self.access.signed_in()})
            if path == "/api/live":
                return self._json(200, p.live())
            if path == "/api/history":
                since = 0
                for part in query.split("&"):
                    if part.startswith("since="):
                        since = int(part[6:] or 0)
                return self._json(200, {"points": p.history_points(since)})
            if path == "/api/scenarios":
                return self._json(200, {"scenarios": p.scenarios()})
            if path == "/api/cloud":
                return self._json(200, p.cloud())
        except ValueError as err:
            return self._json(400, {"error": str(err)})
        return self._json(404, {"error": "not found"})

    # ---- POST ---------------------------------------------------------------------------
    def do_POST(self):  # noqa: N802
        if self.headers.get(ACTION_HEADER) != "1":
            return self._json(403, {"error": f"missing {ACTION_HEADER} header"})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._json(415, {"error": "Content-Type must be application/json"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "body too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except ValueError as err:
            return self._json(400, {"error": f"bad JSON: {err}"})

        if self.path == "/api/login":
            return self._login(body)
        if self.path == "/api/logout":
            user = self.access.session_of(cookie_value(self.headers.get("Cookie")), touch=False)
            self.access.sign_out(cookie_value(self.headers.get("Cookie")))
            if user:
                self.panel._log(f"[{user.name}] signed out")
            return self._json(200, {"ok": True}, self._cookie("", 0))

        user = self._user()
        if user is None:
            return self._json(401, {"error": "login required", "login": "/login"})
        if user.role != OPERATOR:
            return self._json(403, {"error": "View only: your PIN is a viewer PIN, so the controls are locked"})
        p, actor = self.panel, self._actor(user)
        try:
            if self.path == "/api/action":
                return self._json(200, p.action(str(body.get("action", "")), body.get("value"), actor))
            if self.path == "/api/scenario/start":
                return self._json(200, p.start_scenario(str(body.get("name", "")), actor))
            if self.path == "/api/scenario/stop":
                return self._json(200, p.stop_scenario(actor))
        except (ValueError, TypeError) as err:
            return self._json(400, {"ok": False, "error": str(err)})
        return self._json(404, {"error": "not found"})

    def _login(self, body: dict) -> None:
        if not self.access.sharing:
            return self._json(200, {"ok": True, "role": OPERATOR, "sharing": False})
        client = self._client()
        wait = self.access.locked_for(client)
        if wait:
            return self._json(429, {"error": f"Too many wrong PINs. Try again in {wait} s."},
                              {"Retry-After": str(wait)})
        user = self.access.login(str(body.get("pin", "")), client)
        if user is None:
            log.warning("[panel] wrong PIN from %s", client)
            return self._json(401, {"error": "Wrong PIN"})
        self.panel._log(f"[{user.name}] signed in ({user.role})")
        log.info("[panel] %s signed in (%s)", user.name, user.role)
        return self._json(200, {"ok": True, "role": user.role, "name": user.name},
                          self._cookie(self.access.issue(user), self.access.session_s))

    def do_OPTIONS(self):  # noqa: N802 - never approve cross-origin requests
        self._json(403, {"error": "cross-origin requests are not allowed"})


def serve(panel: Panel, host: str = "127.0.0.1", port: int = 8080,
          access: Access | None = None) -> ThreadingHTTPServer:
    access = access or Access()
    handler = type("PanelHandler", (_Handler,), {"panel": panel, "access": access})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="panel-http", daemon=True).start()
    panel.start_sampler()
    log.info("Web panel on http://%s:%d  (Codespaces: Ports tab -> 8080 -> Open in Browser)",
             "localhost" if host in ("127.0.0.1", "0.0.0.0") else host, httpd.server_address[1])
    if access.sharing:
        people = ", ".join(f"{u['name']} ({u['role']})" for u in access.roster())
        log.info("Panel sharing is on for: %s. To share, set port 8080 to Public in the Ports tab "
                 "and send the link.", people)
    elif host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("Web panel listens on %s with no PINs: set EMS_PANEL_USERS before exposing it", host)
    return httpd
