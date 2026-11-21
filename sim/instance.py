"""Stop a second copy of the simulator before it connects.

IoT Hub allows one connection per device ID. Two simulators using the same ID knock each
other off and reconnect over and over; every reconnect costs messages, and on the free
tier that burned the whole 8,000-message day in minutes (21 Nov 2026). So:
- a lock file in the state folder lets only one simulator run per workspace;
- the panel's port is checked before connecting, so a busy port stops the run instead of
  being noticed after the device is already online.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

try:
    import fcntl
except ImportError:          # Windows: no advisory locks; the port check still applies
    fcntl = None


class InstanceLock:
    def __init__(self, state_dir: Path, name: str = "simulator.lock"):
        self.path = Path(state_dir) / name
        self._fh = None
        self.other_pid: int | None = None

    def acquire(self) -> bool:
        """True if this process now holds the lock; False if another simulator does."""
        if fcntl is None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")  # noqa: SIM115 - kept open for the life of the process
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            text = fh.read().strip()
            self.other_pid = int(text) if text.isdigit() else None
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh            # the lock lasts until this process exits
        return True

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def port_free(host: str, port: int) -> bool:
    """Can we listen on host:port right now? (Port 0 always can.)"""
    if not port:
        return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host if host not in ("localhost",) else "127.0.0.1", port))
        except OSError:
            return False
    return True
