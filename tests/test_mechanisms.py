"""Final project: mechanisms that had no unit test yet, and the exact configuration
difference between the three builds (brief, section 5: "document exactly which
mechanisms differ from the baseline").

Already covered elsewhere: circuit breaker, backoff with jitter, bulkhead
(test_resilience), mirrored storage with checksums (test_storage), duplicate-write
detection and epoch fencing (test_dbnode), database least privilege (test_dbnode_acl),
authentication/authorization/validation (test_security), idempotent payments end to
end (test_integration).
"""
from dataclasses import asdict

import pytest

from ftuni import config
from ftuni.services.payment import PENDING_EXPIRY, recovery_action

CLIENT_TIMEOUT = 5.0         # experiments/workload.py: how long a student waits for a page
DB_FAILOVER_BUDGET = 1.0     # monitor: 3 missed checks at 0.25 s + promotion (measured 0.5-0.7 s)

RESILIENCE = {"gw_timeout", "gw_retries", "health_checks", "breaker", "load_shedding", "degrade",
              "db_failover", "db_timeout", "db_deadline", "idempotency", "bank_timeout", "bank_retries",
              "recovery_worker", "mirror_storage", "checkpointing", "restart_delay", "db_restart_delay"}
SECURITY = {"auth", "validation", "rate_limit", "db_acl", "protect_ops", "bind_idempotency", "audit"}


# ------------------------------------------------------------ rollback / recovery rules

@pytest.mark.parametrize("status,debit,age,expected", [
    ("QUEUED", None, 0.0, "settle"),                        # accepted while the bank was down
    ("QUEUED", None, 999.0, "settle"),
    ("PENDING", 200, 0.5, "roll_forward"),                  # money moved: finish the bookkeeping
    ("PENDING", 200, 999.0, "roll_forward"),
    ("PENDING", 404, PENDING_EXPIRY + 0.1, "roll_back"),    # no debit, client gone
    ("PENDING", 404, PENDING_EXPIRY - 0.1, "wait"),         # the client may still retry
    ("PENDING", 503, 999.0, "wait"),                        # bank unsure: never guess
    ("PENDING", None, 999.0, "wait"),
])
def test_recovery_rules(status, debit, age, expected):
    assert recovery_action(status, debit, age) == expected


def test_recovery_never_rolls_back_money_that_moved():
    for age in (0.0, PENDING_EXPIRY, 100 * PENDING_EXPIRY):
        assert recovery_action("PENDING", 200, age) == "roll_forward"


# ------------------------------------------------------------ timeouts (nested budgets)

@pytest.mark.parametrize("pol", [config.FT, config.ENDTERM], ids=["ft", "endterm"])
def test_payment_timeout_budget_is_nested(pol):
    """Worst case inside the payment service (every bank attempt times out, plus the
    jittered backoff between attempts) fits in the gateway's payment timeout, which fits
    in the client's patience - so a retry never races the original request."""
    attempts = 1 + pol.bank_retries
    worst_backoff = sum(min(1.0, 0.1 * 2 ** a) for a in range(pol.bank_retries))  # backoff_delay(a, 0.1, 1.0)
    inner = attempts * pol.bank_timeout + worst_backoff
    assert inner < pol.timeout("payment") < CLIENT_TIMEOUT


@pytest.mark.parametrize("pol", [config.FT, config.ENDTERM], ids=["ft", "endterm"])
def test_gateway_timeouts_outlast_a_failover_and_fit_the_client(pol):
    for kind in ("student", "payment", "records", "timetable"):
        t = pol.timeout(kind)
        assert t < CLIENT_TIMEOUT
        if kind != "timetable":           # the timetable is computed; it never waits for the database
            assert t > DB_FAILOVER_BUDGET + pol.db_timeout
    assert pol.db_timeout < pol.db_deadline


def test_baseline_timeouts_outlive_the_client():
    """The midterm finding behind E3/X3: library-default timeouts are longer than a user's
    patience, so the user retries while the first request is still running."""
    assert config.BASELINE.timeout("payment") > CLIENT_TIMEOUT
    assert config.BASELINE.bank_timeout > CLIENT_TIMEOUT


# ------------------------------------------------------------ what differs between the builds

def changed(a: config.Policy, b: config.Policy) -> set[str]:
    da, db = asdict(a), asdict(b)
    return {k for k in da if k != "name" and da[k] != db[k]}


def test_ft_differs_from_baseline_in_every_resilience_setting_and_nothing_else():
    assert changed(config.BASELINE, config.FT) == RESILIENCE


def test_endterm_is_ft_plus_exactly_the_seven_security_controls():
    assert changed(config.FT, config.ENDTERM) == SECURITY
    assert all(getattr(config.ENDTERM, f) is True for f in SECURITY)
    assert not any(getattr(config.FT, f) for f in SECURITY)
    assert not any(getattr(config.BASELINE, f) for f in SECURITY)


def test_only_the_replicated_builds_have_two_copies():
    for kind in ("db", "student", "payment", "records", "timetable"):
        assert len(config.of_kind(kind, "baseline")) == 1
        assert len(config.of_kind(kind, "ft")) == len(config.of_kind(kind, "endterm")) == 2
        nodes = {i.node for i in config.of_kind(kind, "endterm")}
        assert nodes == {"A", "B"}       # replicas never share a node (anti-affinity)
