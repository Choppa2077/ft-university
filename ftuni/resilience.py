"""Small resilience toolkit: backoff with jitter, circuit breaker, bulkhead."""
from __future__ import annotations

import random
import time
from typing import Callable, Optional


def backoff_delay(attempt: int, base: float = 0.05, cap: float = 1.0) -> float:
    """Exponential backoff with "full jitter": sleep a random time in [0, base * 2^attempt].

    The jitter matters when many clients retry at once - without it they all come
    back at the same moment and hit the recovering service together.
    """
    return random.uniform(0, min(cap, base * (2 ** attempt)))


class CircuitBreaker:
    """closed -> (N failures in a row) -> open -> (reset_timeout) -> half_open -> one trial call.

    A successful trial closes the breaker, a failed one opens it again.
    """

    def __init__(self, name: str, failure_threshold: int = 5, reset_timeout: float = 3.0,
                 on_change: Optional[Callable[[str, str, str], None]] = None):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.on_change = on_change
        self.state = "closed"
        self.failures = 0
        self.opened_at = 0.0
        self.trial_started = 0.0

    def allow(self) -> bool:
        now = time.monotonic()
        if self.state == "closed":
            return True
        if self.state == "open":
            if now - self.opened_at < self.reset_timeout:
                return False
            self._set("half_open")
            self.trial_started = now
            return True
        # half_open: only one trial at a time. If the trial never reported back
        # (e.g. the request was cancelled) let another one through after a while.
        if now - self.trial_started > self.reset_timeout:
            self.trial_started = now
            return True
        return False

    def success(self) -> None:
        self.failures = 0
        if self.state != "closed":
            self._set("closed")

    def failure(self) -> None:
        if self.state == "half_open":
            self._open()
            return
        self.failures += 1
        if self.state == "closed" and self.failures >= self.failure_threshold:
            self._open()

    def _open(self) -> None:
        self.opened_at = time.monotonic()
        self._set("open")

    def _set(self, new: str) -> None:
        old, self.state = self.state, new
        if self.on_change and old != new:
            self.on_change(self.name, old, new)


class Bulkhead:
    """Caps the number of in-flight calls to one dependency. Never queues: if the
    bulkhead is full the caller is told immediately and can shed or degrade."""

    def __init__(self, limit: int):
        self.limit = limit
        self.inflight = 0

    def try_acquire(self) -> bool:
        if self.inflight >= self.limit:
            return False
        self.inflight += 1
        return True

    def release(self) -> None:
        self.inflight -= 1
