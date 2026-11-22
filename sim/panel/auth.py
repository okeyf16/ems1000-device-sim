"""Sharing the web panel: named PINs, roles, signed sessions, who's signed in.

Sharing is on when people are defined in the Codespace secret EMS_PANEL_USERS:

    EMS_PANEL_USERS = "Okey:operator:482913, Paul:operator:771024, Emeka:viewer:305518"

Each entry is Name:role:PIN. Roles: viewer (read-only) and operator (controls too).
Names appear in "Last actions" and in the "Signed in" list, which everyone can see.
The older pair EMS_PANEL_VIEWER_PIN / EMS_PANEL_OPERATOR_PIN still works (as "Viewer" and
"Operator") until EMS_PANEL_USERS is set. Without either, the panel stays in solo mode.

The session cookie names the person and carries a fingerprint of their PIN, signed with a key
derived from all entries (or EMS_PANEL_SECRET). Removing someone or changing their PIN ends
their session on their next request; everyone else stays signed in, across restarts too.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
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
ACTIVE_S = 15 * 60        # "Signed in" lists people active in the last 15 minutes
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}$")


class AccessError(ValueError):
    """Bad sharing configuration (shown at start-up, never with a PIN in it)."""


@dataclass(frozen=True)
class User:
    name: str
    role: str
    pin: str = field(repr=False, default="")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(f"{self.name}|{self.role}|{self.pin}".encode()).hexdigest()[:16]


SOLO = User("you", OPERATOR)


def parse_users(text: str | None) -> list[User]:
    """'Name:role:PIN, Name:role:PIN' -> users. Raises AccessError naming the bad entry."""
    users: list[User] = []
    for n, raw in enumerate((text or "").split(","), start=1):
        entry = raw.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split(":")]
        if len(parts) != 3:
            raise AccessError(f"EMS_PANEL_USERS entry {n} must be Name:role:PIN")
        name, role, pin = parts[0], parts[1].lower(), parts[2]
        if not NAME_RE.match(name):
            raise AccessError(f"EMS_PANEL_USERS entry {n}: name must be 1-32 letters, digits, spaces, . _ -")
        if role not in ROLES:
            raise AccessError(f"EMS_PANEL_USERS entry {n} ({name}): role must be viewer or operator")
        if len(pin) < MIN_PIN_LEN:
            raise AccessError(f"EMS_PANEL_USERS entry {n} ({name}): PIN must be at least {MIN_PIN_LEN} characters")
        users.append(User(name, role, pin))
    names = [u.name.lower() for u in users]
    dup = next((u.name for u in users if names.count(u.name.lower()) > 1), None)
    if dup:
        raise AccessError(f"EMS_PANEL_USERS: the name {dup!r} appears more than once")
    pins = [u.pin for u in users]
    dup_pin_owner = next((u.name for u in users if pins.count(u.pin) > 1), None)
    if dup_pin_owner:
        raise AccessError(f"EMS_PANEL_USERS: {dup_pin_owner} shares a PIN with someone else; PINs must differ")
    return users


def legacy_users(viewer_pin: str | None, operator_pin: str | None) -> list[User]:
    """The two-PIN setup from before named PINs."""
    v, o = (viewer_pin or "").strip(), (operator_pin or "").strip()
    if bool(v) != bool(o):
        raise AccessError("Set both EMS_PANEL_VIEWER_PIN and EMS_PANEL_OPERATOR_PIN (or use EMS_PANEL_USERS)")
    if not v:
        return []
    if len(v) < MIN_PIN_LEN or len(o) < MIN_PIN_LEN:
        raise AccessError(f"Panel PINs must be at least {MIN_PIN_LEN} characters")
    if v == o:
        raise AccessError("The viewer and operator PINs must differ")
    return [User("Viewer", VIEWER, v), User("Operator", OPERATOR, o)]


@dataclass
class Access:
    users: list[User] = field(default_factory=list)
    secret: str | None = None
    session_s: int = SESSION_S
    clock: object = time.time
    _failures: dict = field(default_factory=dict, repr=False)
    _seen: dict = field(default_factory=dict, repr=False)      # name -> last request time
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self):
        material = (self.secret or "").strip() or "ems-panel|" + "|".join(
            sorted(u.fingerprint for u in self.users))
        self._key = hashlib.sha256(material.encode("utf-8")).digest()
        self._by_name = {u.name: u for u in self.users}

    @classmethod
    def from_env(cls, env=os.environ) -> Access:
        if (env.get("EMS_PANEL_USERS") or "").strip():
            users = parse_users(env.get("EMS_PANEL_USERS"))
        else:
            users = legacy_users(env.get("EMS_PANEL_VIEWER_PIN"), env.get("EMS_PANEL_OPERATOR_PIN"))
        return cls(users, env.get("EMS_PANEL_SECRET"))

    @property
    def sharing(self) -> bool:
        return bool(self.users)

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

    def login(self, pin: str, client: str) -> User | None:
        """The person with this PIN, or None. Callers check locked_for() first."""
        given = (pin or "").strip().encode("utf-8")
        match = None
        for u in self.users:   # compare with every PIN, constant time, no early exit
            if hmac.compare_digest(given, u.pin.encode("utf-8")):
                match = u
        with self._lock:
            if match is None:
                self._failures.setdefault(client, []).append(self.clock())
            else:
                self._failures.pop(client, None)
                self._seen[match.name] = self.clock()
        return match

    # ---- sessions --------------------------------------------------------------------
    def _sign(self, payload: str) -> str:
        mac = hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")

    def issue(self, user: User) -> str:
        name = base64.urlsafe_b64encode(user.name.encode("utf-8")).decode("ascii").rstrip("=")
        expiry = int(self.clock()) + self.session_s
        payload = f"{name}.{user.role}.{expiry}.{user.fingerprint}.{secrets.token_urlsafe(6)}"
        return f"{payload}.{self._sign(payload)}"

    def session_of(self, token: str | None, touch: bool = True) -> User | None:
        """The signed-in person for this cookie, or None (bad, expired, removed, PIN changed)."""
        if not token or token.count(".") != 5:
            return None
        payload, _, sig = token.rpartition(".")
        if not hmac.compare_digest(sig, self._sign(payload)):
            return None
        name_b64, role, expiry, fp, _nonce = payload.split(".")
        try:
            name = base64.urlsafe_b64decode(name_b64 + "=" * (-len(name_b64) % 4)).decode("utf-8")
            if int(expiry) < self.clock():
                return None
        except (ValueError, UnicodeDecodeError):
            return None
        user = self._by_name.get(name)
        if user is None or user.role != role or not hmac.compare_digest(fp, user.fingerprint):
            return None
        if touch:
            with self._lock:
                self._seen[user.name] = self.clock()
        return user

    def sign_out(self, token: str | None) -> None:
        user = self.session_of(token, touch=False)
        if user:
            with self._lock:
                self._seen.pop(user.name, None)

    def signed_in(self) -> list[dict]:
        """People active in the last 15 minutes, most recent first (no PINs)."""
        now = self.clock()
        with self._lock:
            rows = [(name, t) for name, t in self._seen.items() if now - t < ACTIVE_S and name in self._by_name]
        rows.sort(key=lambda r: -r[1])
        return [{"name": name, "role": self._by_name[name].role, "idle_s": int(now - t)} for name, t in rows]

    def roster(self) -> list[dict]:
        """Everyone defined, for `cli.py panel-users` (names and roles only)."""
        return [{"name": u.name, "role": u.role} for u in self.users]


def cookie_value(header: str | None, name: str = COOKIE) -> str | None:
    """Value of one cookie from a Cookie header."""
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None
