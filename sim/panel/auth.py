"""Sharing the web panel: viewer and operator PINs, a signed session cookie, login limits.

Sharing is on when both PINs are set (Codespace secrets EMS_PANEL_VIEWER_PIN and
EMS_PANEL_OPERATOR_PIN). Then every page and API call needs a session:
- viewer: everything read-only (gauges, alerts, history, cloud view);
- operator: also start/stop, faults, load, speed and scenarios.
Without PINs the panel stays in solo mode (127.0.0.1 behind the Codespace's private port).

The cookie is "<role>.<expiry>.<nonce>.<hmac>". Its key comes from the PINs (or
EMS_PANEL_SECRET), so sessions survive a simulator or Codespace restart, and changing
either PIN signs everyone out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field

COOKIE = "ems_panel"
VIEWER, OPERATOR = "viewer", "operator"
ROLES = (VIEWER, OPERATOR)
SESSION_S = 12 * 3600
MIN_PIN_LEN = 6
MAX_FAILURES = 5          # wrong PINs per client ...
FAILURE_WINDOW_S = 300    # ... within 5 minutes locks that client out for the rest of the window


class AccessError(ValueError):
    """Bad sharing configuration (shown at start-up)."""


@dataclass
class Access:
    viewer_pin: str | None = None
    operator_pin: str | None = None
    secret: str | None = None
    session_s: int = SESSION_S
    clock: object = time.time
    _failures: dict = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self):
        v, o = (self.viewer_pin or "").strip(), (self.operator_pin or "").strip()
        self.viewer_pin, self.operator_pin = v or None, o or None
        if bool(v) != bool(o):
            raise AccessError("Set both EMS_PANEL_VIEWER_PIN and EMS_PANEL_OPERATOR_PIN to share the panel "
                              "(or neither for solo mode)")
        if v:
            if len(v) < MIN_PIN_LEN or len(o) < MIN_PIN_LEN:
                raise AccessError(f"Panel PINs must be at least {MIN_PIN_LEN} characters")
            if v == o:
                raise AccessError("The viewer and operator PINs must differ")
        material = (self.secret or "").strip() or f"ems-panel|{v}|{o}"
        self._key = hashlib.sha256(material.encode("utf-8")).digest()

    @classmethod
    def from_env(cls) -> Access:
        return cls(os.getenv("EMS_PANEL_VIEWER_PIN"), os.getenv("EMS_PANEL_OPERATOR_PIN"),
                   os.getenv("EMS_PANEL_SECRET"))

    @property
    def sharing(self) -> bool:
        return self.viewer_pin is not None

    # ---- login -----------------------------------------------------------------------
    def locked_for(self, client: str) -> int:
        """Seconds this client must wait before trying again (0 = may try)."""
        now = self.clock()
        with self._lock:
            recent = [t for t in self._failures.get(client, []) if now - t < FAILURE_WINDOW_S]
            self._failures[client] = recent
            if len(recent) >= MAX_FAILURES:
                return int(FAILURE_WINDOW_S - (now - recent[0])) + 1
        return 0

    def login(self, pin: str, client: str) -> str | None:
        """Role for this PIN, or None. Callers check locked_for() first."""
        pin = (pin or "").strip().encode("utf-8")
        role = None
        # Compare against both PINs every time (constant-time, no early exit)
        if hmac.compare_digest(pin, (self.operator_pin or "").encode("utf-8")):
            role = OPERATOR
        if hmac.compare_digest(pin, (self.viewer_pin or "").encode("utf-8")):
            role = role or VIEWER
        if role is None:
            with self._lock:
                self._failures.setdefault(client, []).append(self.clock())
        else:
            with self._lock:
                self._failures.pop(client, None)
        return role

    # ---- sessions --------------------------------------------------------------------
    def _sign(self, payload: str) -> str:
        mac = hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")

    def issue(self, role: str) -> str:
        if role not in ROLES:
            raise ValueError(role)
        payload = f"{role}.{int(self.clock()) + self.session_s}.{secrets.token_urlsafe(8)}"
        return f"{payload}.{self._sign(payload)}"

    def role_of(self, token: str | None) -> str | None:
        if not token or token.count(".") != 3:
            return None
        payload, _, sig = token.rpartition(".")
        if not hmac.compare_digest(sig, self._sign(payload)):
            return None
        role, expiry, _nonce = payload.split(".")
        try:
            if int(expiry) < self.clock():
                return None
        except ValueError:
            return None
        return role if role in ROLES else None


def cookie_value(header: str | None, name: str = COOKIE) -> str | None:
    """Value of one cookie from a Cookie header."""
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None
