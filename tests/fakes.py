"""Shared test doubles."""


class FakeTimers:
    """Deterministic stand-in for threading.Timer: fire() runs the pending callback."""

    def __init__(self):
        self.pending = []

    def __call__(self, seconds, fn):
        timers = self

        class _T:
            daemon = False
            cancelled = False

            def __init__(self):
                self.seconds, self.fn = seconds, fn

            def start(self):
                timers.pending.append(self)

            def cancel(self):
                self.cancelled = True

        return _T()

    def fire(self):
        live = [t for t in self.pending if not t.cancelled]
        assert live, "no pending timer"
        t = live[-1]
        self.pending.remove(t)
        t.fn()
        return t.seconds
