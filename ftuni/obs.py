"""Observability shared by every process: request/correlation IDs and Prometheus metrics.

* Every request gets an ID at the first hop (the gateway). It travels in the
  X-Request-ID header to the service, the database node and the bank, is written
  into every log line and audit record, and is returned to the client. One ID is
  enough to follow a request through all containers (experiments/trace.py).
* Metrics use the default prometheus_client registry (one process per container)
  and are exposed on /metrics by common.make_app.
* Every structured event (EventLog) is also counted in ftuni_events_total{event},
  so any log event can drive an alert rule without writing a dedicated counter.
"""
from __future__ import annotations

import contextvars
import re
import uuid

import httpx
from prometheus_client import Counter, Gauge, Histogram

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
actor: contextvars.ContextVar[str] = contextvars.ContextVar("actor", default="")

_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def new_request_id(incoming: str | None) -> str:
    """Accept a client-supplied ID only if it is harmless (no log injection), else make one."""
    if incoming and _VALID_ID.match(incoming):
        return incoming
    return uuid.uuid4().hex[:16]


async def propagate_request_id(request: httpx.Request) -> None:
    """httpx event hook: outgoing calls carry the ID of the request being served."""
    rid = request_id.get()
    if rid and "x-request-id" not in request.headers:
        request.headers["X-Request-ID"] = rid


LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0)

HTTP_REQUESTS = Counter("ftuni_http_requests_total", "HTTP requests answered",
                        ["service", "route", "method", "status"])
HTTP_LATENCY = Histogram("ftuni_http_request_duration_seconds", "Time to answer a request",
                         ["service", "route"], buckets=LATENCY_BUCKETS)
HTTP_INFLIGHT = Gauge("ftuni_http_inflight", "Requests being processed", ["service"])
EVENTS = Counter("ftuni_events_total", "Structured log events by type", ["event"])
BREAKER_OPEN = Gauge("ftuni_breaker_open", "1 while a circuit breaker is open or half-open", ["target"])
AUTH_DENIED = Counter("ftuni_auth_denied_total", "Requests refused by authentication/authorization",
                      ["reason"])
RATE_LIMITED = Counter("ftuni_rate_limited_total", "Requests refused by the rate limiter", ["scope"])
DB_ROLE = Gauge("ftuni_db_is_primary", "1 if this database node is the primary")
DB_EPOCH = Gauge("ftuni_db_epoch", "Current epoch (fencing token) of this database node")
DB_LSN = Gauge("ftuni_db_lsn", "Last applied log sequence number")
DB_REPLICATION_OK = Gauge("ftuni_db_replication_ok",
                          "Primary only: 1 if the replica is in sync, 0 if writes are not replicated")
DB_PRIMARY_SEEN = Gauge("ftuni_dbclient_epoch", "Epoch of the primary this client is using")
PAYMENTS_UNFINISHED = Gauge("ftuni_payments_unfinished", "Payments in PENDING or QUEUED state", ["status"])
MONITOR_TARGET_UP = Gauge("ftuni_monitor_target_up", "Monitor view: 1 if the instance answers /health/ready",
                          ["target", "kind", "node"])
AUDIT_RECORDS = Counter("ftuni_audit_records_total", "Audit records written", ["event", "outcome"])

# Counters only appear in /metrics after their first increment, and rate()/increase()
# cannot see that first step. Create the series that alerts depend on at zero.
for _e in ("DB_FAILOVER", "PROMOTED", "FENCED", "DEMOTED", "LOAD_SHED", "BREAKER", "UPSTREAM_DOWN",
           "ACCESS_DENIED", "VALIDATION_REJECTED", "DB_DENIED", "LOGIN_FAILED", "IDEMPOTENCY_MISMATCH"):
    EVENTS.labels(_e)
for _r in ("missing_token", "bad_signature", "expired", "invalid_token", "invalid_role", "validation",
           "ops_token", "db_token", "db_endpoint", "db_acl", "bad_credentials", "own_record", "own_enrollment",
           "own_payment", "own_transcript", "registrar_only", "no_rule", "invalid_json", "body_not_object",
           "body_too_large"):
    AUTH_DENIED.labels(_r)
for _s in ("user", "login", "lockout"):
    RATE_LIMITED.labels(_s)
DB_REPLICATION_OK.set(1)     # processes that are not database nodes must not look "unreplicated"
