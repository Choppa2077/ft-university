import time

from ftuni.resilience import Bulkhead, CircuitBreaker, backoff_delay


def test_breaker_opens_after_threshold_and_recovers():
    changes = []
    b = CircuitBreaker("x", failure_threshold=3, reset_timeout=0.1, on_change=lambda n, o, s: changes.append(s))
    for _ in range(2):
        assert b.allow()
        b.failure()
    assert b.state == "closed"
    b.failure()
    assert b.state == "open" and not b.allow()
    time.sleep(0.12)
    assert b.allow() and b.state == "half_open"   # one trial call
    assert not b.allow()                          # and only one
    b.success()
    assert b.state == "closed"
    assert changes == ["open", "half_open", "closed"]


def test_failed_trial_reopens():
    b = CircuitBreaker("x", failure_threshold=1, reset_timeout=0.05)
    b.failure()
    time.sleep(0.06)
    assert b.allow()
    b.failure()
    assert b.state == "open" and not b.allow()


def test_success_resets_failure_count():
    b = CircuitBreaker("x", failure_threshold=3)
    b.failure(); b.failure(); b.success(); b.failure(); b.failure()
    assert b.state == "closed"


def test_backoff_has_jitter_and_cap():
    vals = [backoff_delay(5, base=0.1, cap=1.0) for _ in range(200)]
    assert all(0 <= v <= 1.0 for v in vals)
    assert len(set(round(v, 4) for v in vals)) > 50


def test_bulkhead_rejects_when_full():
    b = Bulkhead(2)
    assert b.try_acquire() and b.try_acquire()
    assert not b.try_acquire()
    b.release()
    assert b.try_acquire()
