"""Single-key shortcuts in the simulator terminal (no Enter needed).

Puts the terminal in cbreak mode (keys arrive one at a time, Ctrl+C still works) and
restores it on exit. Does nothing when stdin is not a terminal; --headless skips it.
"""

from __future__ import annotations

import atexit
import logging
import select
import sys
import threading

from .controls import Controls, handle_key, help_text

log = logging.getLogger(__name__)


def start_keyboard(controls: Controls, stop_event: threading.Event) -> threading.Thread | None:
    if not sys.stdin.isatty():
        log.info("No terminal attached: keyboard shortcuts off")
        return None
    try:
        import termios
        import tty
    except ImportError:  # Windows: use the Codespace or WSL
        log.warning("Keyboard shortcuts need a Linux/macOS terminal; they are off")
        return None

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)

    def restore():
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except Exception:  # noqa: BLE001
            pass

    tty.setcbreak(fd)
    atexit.register(restore)

    def loop():
        log.info("%s", help_text())
        try:
            while not stop_event.is_set():
                ready, _, _ = select.select([sys.stdin], [], [], 0.2)
                if not ready:
                    continue
                key = sys.stdin.read(1)
                if not key:
                    break
                msg = handle_key(controls, key)
                if msg:
                    log.info("%s", msg)
        finally:
            restore()

    t = threading.Thread(target=loop, name="keyboard", daemon=True)
    t.start()
    return t
