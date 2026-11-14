# Verification and validation plan (final project)

Work package H of the final brief. Written before the final campaign; the pass/fail criteria
are the exact predicates coded in `experiments/final.py` / `experiments/endterm.py`.

## 1. Levels of testing

| Level | What | How to run | Count |
|---|---|---|---|
| Unit | breaker, backoff, bulkhead, mirrored storage, DB deduplication and fencing, DB least privilege, JWT/authorization/validation, recovery rules, timeout budgets, build configurations | `python -m pytest -q tests` (no cluster needed) | 58 |
| Integration | login required, wrong password, happy path register→pay→transcript, request-ID propagation, idempotent payment charged once, idempotency key bound to the body, IDOR blocked, role separation | `python -m pytest -q tests/test_integration.py` against a running `endterm` cluster | 10 |
| System / fault injection | experiments X0–X9 (below), three builds, repeated | `python -m experiments.final` | 10 experiments |
| Reliability | soak test with random crashes → MTTF, MTTR, MTBF, availability, failure rate | `python -m experiments.soak_docker` | 2 builds × 900 s |

## 2. Builds compared

`baseline` (midterm baseline: one copy of everything, generous timeouts, no retries, no
idempotency, manual repair after 20 s), `ft` (midterm fault-tolerant build: replicas,
replication + failover, timeouts, retries, breaker, idempotency, checkpoint, recovery worker,
degradation) and `endterm` (= `ft` + seven security controls). Exactly which settings differ is
asserted by `tests/test_mechanisms.py` (`ft` differs from `baseline` in all 17 resilience
settings and nothing else; `endterm` differs from `ft` in exactly the 7 security flags).
Observability (metrics, request IDs, structured logs, alert rules) is present in all three
builds so that they are measured the same way.

## 3. Procedure (every repetition)

1. Fresh cluster: `docker compose down -v`, new run volume, `up`, wait until the gateway sees
   every replica, Prometheus scrapes every instance, the monitor sees every instance ready and
   no alert fires.
2. Open-loop workload, 40 req/s, fixed mix and seed (same requests in every build); in the
   `endterm` build the 300 synthetic students log in first.
3. Inject the fault at t = 10 s through the Docker API (kill, pause, network disconnect) or the
   operator-token chaos switch run *inside* the target container — never through a back door.
4. Wait for the end of the load plus a settle time, so the consistency check runs on a quiet
   system.
5. Evidence: every request (`requests.csv`), every structured event from every container
   (`events.jsonl`), alerts as delivered to the pager (alert-sink), Prometheus queries
   (core p95, gateway counters, CPU, memory), audit chain verification, consistency check.
6. Score against the acceptance criterion → `summary.json`; aggregates over repetitions.

Repetitions: 5 per build for X0–X4 and X7–X9; 3 for X5 and X6 (endterm only). A repetition that
fails because of the *harness* (e.g. the cluster does not become ready) is retried on a fresh
cluster and logged in `results/final/harness_retries.jsonl`; system results are never retried.

## 4. Experiments

| ID | Brief matrix | Scenario and injection | Run | Hypothesis | PASS criterion (coded) |
|---|---|---|---|---|---|
| X0 | 6 (monitoring), trade-offs | no fault | 120 s | the improved build costs a few ms of latency and some CPU/memory, and raises no alert when nothing is wrong | every request succeeds **and** no alert fires **and** data consistent |
| X1 | 1 | `kill -9 -1` inside `payment-a` | 40 s | the gateway masks the crash; Docker restarts the replica | 0 failed requests after the injection **and** consistent |
| X2 | 2 | `docker pause` the database primary for 12 s | 45 s | the monitor fails over within ~1 s; requests wait, none fail; money consistent | 0 failed **and** consistent |
| X3 | 3 | bank answers 6 s late (health included) for 15 s | 45 s | bounded timeouts + breaker + queue: no failures, no double charge | 0 failed **and** consistent |
| X4 | 5 | +180 timetable req/s for 15 s (≈5×) | 45 s | the bulkhead sheds timetable load; core p95 stays < 1 s | core p95 < 1000 ms **and** consistent |
| X5 | 7 | 17 unauthorized / privilege-escalation probes incl. a compromised container | — | all refused, data unchanged, audited | all refused **and** checksum unchanged **and** audit chain verifies |
| X6 | 7 | 10 malformed / abusive probes + a request flood | — | rejected with 4xx/429, service up, data unchanged | all rejected, flood throttled **and** checksum unchanged **and** audit chain verifies |
| X7 | 4, 8 | 30 % of payments abort right after the bank debit, for 15 s | 40 s | users see errors in every build, but only the improved builds keep money consistent | consistent |
| X8 | 2, 8 | `docker network disconnect` the primary from the data network for 12 s | 45 s | failover without split brain; consistent after the partition heals | consistent |
| X9 | 6 (monitoring) | `student-a` answers 50 % of requests with HTTP 500, health stays green; runbook restarts the replica named by the alert | 55 s | redundancy does not mask a gray failure; telemetry detects it and names the right replica | alert names `student-a` within 15 s **and** consistent |

"Failed requests" counts every client request that did not get a 2xx after the injection (a
payment and its retry are two requests). User-level counts (a payment and its retry = one
action) are computed in the analysis.

## 5. Alert expectations (for alert correctness)

An alert is **related** if it concerns the injected component or a direct consequence of the
fault; anything else is a **false positive**. An incident that lasts at least one alert window
(10 s) must raise at least one related alert (R9).

| ID | Related alerts | Alert required? |
|---|---|---|
| X0 | none (every alert is a false positive) | no |
| X1 | `InstanceDown{payment-a}`, `UserFacingErrorRate{payment}`, `ReplicaErrorRate{payment-*}`, `HighLatencyP95{payment}` | baseline only (20 s outage); masked in ft/endterm |
| X2, X8 | `InstanceDown{db-*}`, `DatabaseFailover`, `DBReplicationLost`, `HighLatencyP95`/`UserFacingErrorRate` of student, payment, records, `ReplicaErrorRate` of their replicas | yes |
| X3 | `InstanceDown{bank}`, `CircuitBreakerOpen{→bank}`, `PaymentsUnfinished`, payment `HighLatencyP95`/`UserFacingErrorRate`/`ReplicaErrorRate` | yes |
| X4 | `LoadShedding`, `HighLatencyP95`, `UserFacingErrorRate`, `ReplicaErrorRate`, `InstanceDown` (overload is system-wide in the baseline) | yes |
| X7 | `PaymentsUnfinished`, payment `UserFacingErrorRate`/`ReplicaErrorRate`/`HighLatencyP95` | yes |
| X9 | `ReplicaErrorRate{student-a}`, `UserFacingErrorRate{student}`, `HighLatencyP95{student}`, `InstanceDown{student-a}` (after the runbook restart); `ReplicaErrorRate` naming another replica = **wrong diagnosis** | yes |
| X5, X6 | `AuthFailureSpike`, `RateLimitSpike`, `InvalidInputSpike` | — |

## 6. Measurements recorded (brief, section 7)

Per repetition: requests, successes, failures (after injection and in total), success ratio,
p50/p95/p99 client latency, core p95 at the gateway, detection time by event (gateway/monitor)
and by alert (pager), user-visible recovery time, container restarts, alerts fired with labels
and timing, **mechanism activations** (gateway retries, load shed, stale answers, fast-fail,
replicas marked down, breaker openings, bank retries, queued payments, recovery-worker roll
forward/back, compensations, duplicate requests detected, database failovers, client switches,
fencing, resyncs), CPU and memory of the application processes, and the consistency check
(orphan debits, double charges, ledger mismatches, stuck payments, lost acknowledged writes,
replica identical). Soak: per-component and per-service failures, downtime, MTTF, MTTR, MTBF,
availability, failure rate, and the RBD prediction from the measured component availabilities.

## 7. Mapping to the brief's minimum experiment matrix

| Brief # | Scenario | Experiments |
|---|---|---|
| 1 | application/service instance failure | X1 (+ soak) |
| 2 | database or dependency unavailable | X2, X8 |
| 3 | network delay or timeout | X3 |
| 4 | interrupted multi-step transaction | X7 |
| 5 | high load or traffic spike | X4 |
| 6 | monitoring and alert trigger | X9, X0 (+ alert correctness over all runs) |
| 7 | unauthorized or invalid operation | X5, X6 |
| 8 | recovery/rollback or failover verification | X2, X8 (failover), X7 (roll forward/back) |
