# Fault model, failure-impact analysis and single points of failure (final project)

Work package A/C of the final brief. The threat model for malicious faults is in
`docs/endterm/THREAT_MODEL.md` (STRIDE, 15 threats); this document covers all fault classes
together and connects every scenario to the mechanism that handles it and the experiment that
verifies it.

## 1. Failure scenarios: fault → error → failure

| ID | Fault (class) | Error inside the system | Failure without protection (baseline) | Where the improved build breaks the chain | Verified by |
|---|---|---|---|---|---|
| F1 | service process crash (crash) | instance gone, requests in flight lost | 5xx for every request routed to it until a manual restart (~20 s) | gateway: refused connection → replica marked down, request retried on the other replica; Docker restart policy | X1, soak |
| F2 | database primary crash (crash) | no node accepts writes; a payment already sent to the bank cannot be recorded | all writes fail; debits without payment records; double charges on retry | monitor promotes the in-sync replica (epoch = fencing token); clients find the new primary; PENDING checkpoint + idempotency | soak (db kills); midterm E2 |
| F3 | database primary frozen (timing) | process alive but silent; clients hang | writes time out; same money inconsistency as F2 | as F2; timeouts bound the wait (1 s per try, 4 s deadline) | X2 |
| F4 | network partition between primary and services (omission) | both nodes alive, cannot talk; risk of split brain | as F2; on rejoin, two "primaries" could accept writes | failover + epoch fencing: the old primary's writes are rejected and it steps down | X8 |
| F5 | slow external bank (timing) | payment requests pile up | client times out at 5 s and retries → charged twice | 0.8 s timeout, 2 retries with the same key, circuit breaker, payment accepted as QUEUED (202) and finished later | X3 |
| F6 | crash in the middle of a payment (crash mid-transaction) | debit exists, payment row missing or PENDING | money taken without a record; a retry charges again | PENDING checkpoint before the bank call; the retry (same key) or the recovery worker rolls forward/back | X7 |
| F7 | load spike, ~5× (overload) | queues grow, CPU saturated | timeouts in every service; does not recover after the burst (metastable) | per-service bulkhead sheds load, stale timetable cache, replicas | X4 |
| F8 | gray failure: a replica returns HTTP 500 while its health checks pass (value/partial) | one replica wrong, the routing layer believes it is healthy | a share of the requests fails — in **both** builds (redundancy does not help: the bad replica is never removed) | **observability only**: per-replica 5xx ratio and the `ReplicaErrorRate` alert name the replica; on-call runbook restarts it | X9 |
| F9 | silent corruption of stored transcripts (value) | wrong bytes on disk | wrong GPA served with HTTP 200 | SHA-256 per document, read from the mirror, repair, scrub | midterm E7; `tests/test_storage.py` |
| F10 | unauthorized, forged, malformed or abusive requests (malicious) | requests that should never be executed | anyone reads or changes anything (no authentication at all) | JWT authentication, role + object authorization, validation, rate limiting, audit log | X5, X6 |
| F11 | compromised service container with its own DB credential (malicious insider) | attacker speaks to the database directly | full database access, DDL, forced promotion | per-service DB token + SQLite authorizer (least privilege), endpoint ACL (only the monitor may promote) | X5 (insider cases) |
| F12 | control-plane failure: gateway, monitor, Docker host | entry point or failover controller gone | complete outage / no failover | **not mitigated** — single points of failure (section 4); Docker restarts the gateway/monitor container | documented, not injected |

## 2. Failure-impact analysis (FMEA)

S = severity of the user-visible failure, O = how often the fault occurs, D = how hard it is to
detect (1 = detected immediately, 5 = not detected). Scale 1–5. **The ratings are engineering
judgement, not measurements**; the measured evidence for each row is in the "verified by"
column of section 1 and in the final report. RPN = S × O × D.

| ID | Failure mode | S base → impr. | O | D base → impr. | RPN base → impr. | Residual risk in the improved build |
|---|---|---|---|---|---|---|
| F1 | service outage after a crash | 3 → 1 | 4 | 1 → 1 | 12 → 4 | both replicas of one service crash within ~1 s of each other (seen once in the midterm soak) |
| F2 | DB primary loss, money inconsistent | 5 → 2 | 2 | 1 → 1 | 10 → 4 | monitor down at the same time (cut set {monitor, primary}) |
| F3 | DB freeze, writes hang | 5 → 2 | 2 | 2 → 1 | 20 → 4 | ~1 s latency spike during failover (HighLatencyP95 fires briefly) |
| F4 | partition, split brain | 5 → 2 | 2 | 2 → 1 | 20 → 4 | requires the monitor on the majority side; no quorum (single controller) |
| F5 | slow bank, double charges | 4 → 2 | 3 | 2 → 2 | 24 → 12 | payments complete late (eventual consistency for the student) |
| F6 | interrupted payment, orphan debit | 5 → 2 | 2 | 5 → 2 | 50 → 8 | the student still sees an error even though the payment completes a second later |
| F7 | overload collapse | 4 → 2 | 3 | 2 → 2 | 24 → 12 | timetable answers may be stale; uncached group/week combinations fail fast |
| F8 | gray failure | 3 → 3 | 2 | 4 → 2 | 24 → 12 | users see errors until a human (or the runbook) acts: there is no automatic outlier ejection |
| F9 | wrong transcript shown | 5 → 1 | 1 | 5 → 1 | 25 → 1 | both mirrored copies live on the same Docker volume (common mode) |
| F10 | unauthorized access / abuse | 5 → 1 | 3 | 5 → 2 | 75 → 6 | stolen valid student token; brute force slowed, not prevented |
| F11 | compromised container | 5 → 2 | 1 | 5 → 2 | 25 → 4 | the compromised service's own tables remain writable |
| F12 | control-plane failure | 5 → 5 | 1 | 1 → 1 | 5 → 5 | unchanged — the main remaining weakness |

## 3. Failure-impact by workflow

| Workflow | Worst consequence | Scenarios that threaten it | Protected by |
|---|---|---|---|
| tuition payment | money taken without a record, or charged twice | F2–F6, F10 | checkpoint + idempotency key end-to-end, failover with fencing, breaker + queue, recovery worker, authorization |
| course registration | lost or duplicated enrollment | F1–F4, F10 | replicas, synchronous replication, idempotent insert, authorization |
| transcript | wrong grade shown / disclosed to another student | F9, F10, F11 | checksums + mirror, object-level authorization, DB least privilege |
| timetable | unavailable or stale | F1, F7 | replicas, bulkhead, stale cache |

## 4. Single points of failure and minimal cut sets

Top event: **"a student cannot submit a tuition payment"**.

| Order | Baseline | Improved (`endterm`) |
|---|---|---|
| 1 | {gateway}, {payment}, {database}, {bank}, {host} | {gateway}, {host}, {shared `run` volume} — the bank is no longer a cut set for *submitting* a payment (it is accepted as QUEUED) |
| 2 | — | {payment-a, payment-b}, {db-a, db-b}, {monitor, database primary} |

Remaining single points of failure, and why they were accepted:

- **Gateway** (one container). Docker restarts it in about a second, but while it is down
  nothing is reachable. Fix: two gateways behind a virtual IP (keepalived/VRRP) or DNS.
- **Monitor** (the only failover controller). Not on the request path, but if it is down when
  the primary fails, nobody promotes the replica. Fix: a consensus-based controller (etcd,
  Patroni) with three members.
- **One Docker host.** "Nodes" A and B are containers on the same VM; a host failure takes
  everything down. Fix: two hosts (or Kubernetes nodes) with anti-affinity.
- **Shared `run` volume** (registry of the current primary, logs, audit log, both transcript
  "disks"). The mirroring protects against corrupted files, not against losing the volume
  (common mode). Fix: separate volumes on separate disks; registry in the coordinator.
- **Bank** (external, not replicable): mitigated by idempotent debits, breaker and queueing.
- **Prometheus/Alertmanager**: not on the request path; if they fail, incidents are still
  handled automatically but no longer reported (detection SPOF, not availability SPOF).
