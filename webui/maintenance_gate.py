"""Atomic admission and drain accounting; no policy/database schema changes."""
import threading


class MaintenanceGate:
    def __init__(self):
        self._lock = threading.Lock()
        self._paused = False
        self._active = 0

    def begin(self, continuation=False):
        with self._lock:
            if self._paused and not continuation:
                return False
            self._active += 1
            return True

    def end(self):
        with self._lock:
            if self._active <= 0:
                raise RuntimeError("unbalanced maintenance lease")
            self._active -= 1

    def set_paused(self, paused):
        if not isinstance(paused, bool):
            raise ValueError("paused must be a boolean")
        with self._lock:
            self._paused = paused
        return self.snapshot()

    def snapshot(self):
        with self._lock:
            return {"paused": self._paused, "active": self._active,
                    "drained": self._paused and self._active == 0}
