# Requirements, assumptions and dependability objectives (final project)

Work package A of the final brief. The service-level objectives (SLOs) and the acceptance
criteria below were fixed **before** the final experiment campaign was run (the brief,
section 5: "specify ... success criteria before running each experiment"). They tighten the
midterm requirements R1–R6 and the endterm requirements R7–R12 into measurable objectives
with an indicator, a target, a measurement window and a data source.

## 1. System context

**Users.** About 300 (synthetic) students; staff (registrar, admin); the operator on call.
**External party.** A bank (payment provider) that debits a student's account and returns the
original debit for a repeated idempotency key.

**Services and data.**

| Component | Function | State | Criticality |
|---|---|---|---|
| gateway | single entry point; authentication, authorization, rate limiting, validation; load balancing, retries, breakers, bulkheads, stale cache | none (in-memory cache) | critical path |
| student | profiles, course registration | students, enrollments | high |
| payment | tuition payments through the bank; recovery worker | payments, ledger | **highest (money)** |
| records | transcripts (GPA), end-of-term transcript batch | grades; documents on mirrored storage | high (integrity of records) |
| timetable | weekly timetable of a group (computed, CPU-heavy) | none | low (may degrade) |
| db-a / db-b | SQLite node with synchronous replication, failover by epoch | all business data | critical |
| monitor | health of every instance every 0.25 s; database failover controller | registry (current primary, epoch) | critical for failover |
| Prometheus, Alertmanager, Grafana, alert-sink | metrics, alert rules, dashboard, alert delivery log | metrics | observability (not on the request path) |

**Critical workflow — tuition payment (multi-step, multi-component).** Client → gateway
(JWT, authorization, validation, rate limit) → a healthy payment replica → (1) look up the
Idempotency-Key → (2) write a `PENDING` row = checkpoint, synchronously replicated → (3) debit
at the bank with the same key → (4) write `COMPLETED` + ledger row in one transaction,
replicated → `201`. If the bank is unreachable the payment is accepted as `QUEUED` (`202`) and
finished later by the recovery worker. See `results/final/fig_payment_flow.png`.

**Consequences of failure.** A failed payment page is an annoyance; a debit without a payment
record, or a student charged twice, is a financial and legal problem; a wrong transcript is an
academic-integrity problem; an unavailable timetable is tolerable for minutes.

## 2. Terminology (Avižienis et al.)

- **Fault** — the cause, e.g. a process is killed, the database primary freezes, the bank
  answers after 6 s, a replica starts returning HTTP 500, an attacker sends a forged token.
- **Error** — the resulting wrong internal state, e.g. requests in flight are lost, no node
  accepts writes, payment requests pile up, half of one replica's answers are wrong.
- **Failure** — what the user sees: an HTTP 5xx, a timeout, a double charge, a wrong transcript,
  data disclosed to the wrong student.
- **Fault tolerance** stops the chain between error and failure (masking, failover, retry with
  idempotency, degradation); **observability** shortens the time from error to detection and
  diagnosis; **security controls** prevent a malicious fault from becoming an error at all.

## 3. Requirements carried over (R1–R12)

R1 a single crash fails ≤1% of user requests · R2 a failure is detected within 1 s and the
user-visible interruption is short · R3 no acknowledged write is lost · R4 money is never
inconsistent · R5 under ~5× load the core p95 stays < 1 s · R6 a corrupted transcript is never
shown · R7 every unauthorized action is denied, data unchanged · R8 malformed/abusive input is
rejected, service stays up · R9 every lasting incident raises an alert, detection delay recorded ·
R10 the audit trail is complete and tamper-evident · R11 no service runs with a
missing/placeholder secret · R12 a network partition causes no split brain and no inconsistency.

## 4. Service-level objectives

"Core" = registration + payment (the workflows that move state). Windows are per experiment run
(compressed time, see assumptions). Targets apply to the **improved** build (`endterm`); the
`baseline` and `ft` builds are measured against the same targets for comparison.

| SLO | Indicator (SLI) | Target | Window | Source | Verified by |
|---|---|---|---|---|---|
| **SLO-1** steady-state availability | successful requests / all requests, at the client | ≥ 99.9 % | each 120 s steady run | client request log | X0 |
| **SLO-2** availability under one component fault (R1) | failed requests / all requests after the injection | ≤ 1 % | each run | client request log | X1, X2, X3, X8 |
| **SLO-3** latency | core p95 at the gateway (Prometheus histogram, 20 s windows, max over the run) | ≤ 100 ms in steady state; < 1 s during a 5× load spike (R5) | each run | Prometheus | X0, X4 |
| **SLO-4** detection (R2, R9) | (a) injection → automatic detection by gateway/monitor; (b) injection → first related alert delivered to the pager | (a) ≤ 1 s; (b) ≤ 10 s for incidents lasting ≥ 10 s | per incident | event log; alert-sink | (a) X1, X2, X8; (b) X2, X3, X4, X8, X9 |
| **SLO-5** recovery | user-visible interruption = injection → last failed request | ≤ 2 s for single-instance and database faults | per incident | client request log | X1, X2, X8 |
| **SLO-6** integrity (R3, R4, R12) | orphan debits, double-charged intents, ledger mismatches, stuck payments, lost acknowledged writes; replica identical to primary | all zero / identical, in 100 % of runs | end of each run (quiet system) | consistency check | every fault experiment |
| **SLO-7** security (R7, R8, R10) | refused probes / all probes; business-data checksum before = after; audit chain verifies | 100 %; unchanged; verifies | each run | X5/X6 harness | X5, X6 |
| **SLO-8** long-run availability (compressed time) | fraction of 1 s windows in which every service is up, under random crashes (MTTF 240 s per service process, 480 s per database node) | ≥ 99.5 % | 900 s soak | client request log | soak test |
| **SLO-9** alert quality | (a) alerts in a fault-free run; (b) alert that names the faulty replica in a gray failure | (a) 0 false positives; (b) correct replica, ≤ 15 s | each run | alert-sink, Prometheus `ALERTS` | X0, X9 |

**What is deliberately not an objective.** The baseline is not expected to meet these targets —
it is the "before" measurement. Control-plane availability (gateway, monitor, the single host)
is outside the objectives because those components are known single points of failure (see
`FAULT_MODEL.md`, section 4); they are documented and not tested by killing them.

## 5. Assumptions

- **Fault classes:** crash (process/container), timing (frozen or slow component), omission
  (network partition), value (corrupted storage; a replica returning errors — gray failure),
  overload, and malicious (unauthorized, forged, malformed or abusive requests; a compromised
  service container). Byzantine replicas and common-mode software bugs that hit both replicas
  identically are out of scope.
- **Environment:** one host (Apple M4 Pro, Docker Desktop VM with 12 vCPU and ~7.7 GiB),
  Docker 28.5, one container per instance with CPU/memory limits; containers run Python 3.12.
  Logical nodes A and B are containers, not machines — a power failure of the host takes
  everything down.
- **Compressed time:** faults last 12–20 s, alert windows are 10 s, scrape/evaluation every 1 s,
  the baseline's "manual repair" is 20 s, soak crash rates are far above real ones. Absolute
  MTTF values are only meaningful inside this setup; comparisons between builds are the point.
- **Clients:** open-loop workload (requests sent on schedule regardless of earlier ones),
  40 req/s in experiments (35 % profile reads, 15 % registrations, 20 % payments, 15 %
  transcripts, 15 % timetables), 20 req/s in the soak. A student whose payment fails presses
  "Pay" once more after 1 s; the FT/endterm client reuses the same Idempotency-Key. Client
  timeout 5 s.
- **Control plane** (gateway, monitor, Prometheus/Alertmanager) is not failed in experiments.
- **Data** is synthetic; secrets are generated locally and never committed.
