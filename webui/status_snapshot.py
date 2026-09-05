"""Bounded, single-flight snapshots shared by all HTTP readers."""
from copy import deepcopy
from functools import wraps
import threading
import time


def cached(seconds):
    def decorate(function):
        lock = threading.Lock()
        deadline = 0.0
        value = None

        @wraps(function)
        def read():
            nonlocal deadline, value
            with lock:
                if time.monotonic() >= deadline:
                    value = function()
                    deadline = time.monotonic() + seconds
                return deepcopy(value)
        return read
    return decorate


class SnapshotWorker:
    def __init__(self, producer, interval=2.0):
        self.producer = producer
        self.interval = interval
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.value = None
        self.error = False
        self.thread = None
        self.refresh_lock = threading.Lock()
        self.sampled_at = None

    def refresh(self):
        if not self.refresh_lock.acquire(blocking=False):
            return
        try:
            value = self.producer()
        except Exception:
            with self.lock:
                self.error = True
        else:
            with self.lock:
                self.value = value
                self.sampled_at = time.time()
                self.error = False
        finally:
            self.refresh_lock.release()

    def read(self):
        with self.lock:
            return deepcopy(self.value), self.error

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self.run, name="panel-snapshot", daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop_event.is_set():
            self.refresh()
            value, _ = self.read()
            delay = self.interval if (value or {}).get("process", {}).get("running") else 10
            self.stop_event.wait(delay)

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
