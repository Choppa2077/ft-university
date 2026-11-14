# Resilient University Information System — Endterm Report

**Fault Tolerance and Dependable Computing**

Team: Mukan Idrissov (255567) · Group: CSE-2501M\
Instructor: Azamat Serek\
Repository: https://github.com/Choppa2077/ft-university (branch `endterm`; midterm tagged `midterm`)\
Video: <https://youtu.be/QNMHGkTDlj4>\
Astana IT University, November 2026

---

## Contents

1. Introduction and relationship to the midterm project
2. Updated requirements, assumptions, and threat model
3. Cloud / microservices architecture and resilience design
4. Monitoring, logging, metrics, and alerting design
5. Security controls and intrusion-tolerance strategy
6. Verification and validation plan
7. Fault-injection and security-test methodology
8. Results and baseline-versus-endterm comparison
9. Discussion, limitations, and lessons learned
10. Conclusion
11. References
12. Appendix

---

## 1. Introduction and relationship to the midterm project

The midterm built a small university platform — course registration, tuition payments,
transcripts and timetables — twice from one code base: a **baseline** with a single copy of
everything and ordinary error handling, and a **fault-tolerant (FT)** build that added
replicas, synchronous database replication with automatic failover, mirrored storage, and
the software mechanisms of the course (timeouts, retries with backoff, circuit breakers,
idempotency, checkpointing, rollback, graceful degradation). We broke both builds seven
ways and measured detection time, recovery time, failed requests and data consistency. The
headline result was not about uptime but about correctness: the baseline left money without
records and served silently-wrong transcripts, while the FT build ended every experiment
consistent.

The midterm report also listed, honestly, what it had *not* done (its §11.3). Four items
stood out, and they are exactly the endterm's subject:

> "Everything ran on one machine… we did not depend on Docker or Kubernetes."
> "Each experiment was run once per build."
> "We did not address security (authentication, rate limiting per user)…"
> "a real network partition … was not tested."

So the endterm keeps the same system and the same four services, and extends it along four
axes required by the brief:

1. **Cloud / microservices resilience** — the whole system now runs as one container per
   instance under Docker Compose, with health checks, a restart policy, service discovery
   by DNS name, network segmentation and resource limits. The hand-written supervisor from
   the midterm is gone; its job is now Docker's.
2. **Observability** — every service exposes Prometheus metrics (request rate, error rate,
   latency histograms and more), emits structured logs carrying a correlation ID that is
   propagated across every hop, and is watched by Prometheus + Alertmanager + Grafana. An
   "alert-sink" records exactly when each alert fires, so detection delay is measured, not
   estimated.
3. **Security and intrusion tolerance** — a new class of fault, the *malicious* one. The
   gateway became a policy-enforcement point (JWT authentication, role- and object-level
   authorization, per-user rate limiting, input validation); the database enforces
   least-privilege access per service; all security-relevant actions are written to a
   hash-chained, tamper-evident audit log.
4. **Verification and validation** — eight experiments (X1–X8), each with explicit
   acceptance criteria and PASS/FAIL outcomes, repeated several times, run against the three
   builds (`baseline`, the midterm `ft` configuration, and the new `endterm` configuration)
   in the same containerized environment, with evidence collected automatically from
   Prometheus, the alert sink, the audit log and the data-consistency checks.

The single most important continuity decision: **the endterm is still one code base.** A
`Policy` object (`ftuni/config.py`) decides which mechanisms are on. `baseline` and `ft` are
byte-for-byte the midterm behaviour; `endterm` is `ft` plus the seven security controls and
is the only build with authentication, validation, the database ACL and the audit log. Every
difference in the results therefore comes from configuration, not from different programs.

The short version of the endterm results: on real containers the `ft` and `endterm` builds
still mask single-instance crashes, database-primary loss, a slow dependency and a load
spike with **zero** failed user requests and recovery within a few seconds, while the
baseline loses hundreds of requests and takes ~20 s per incident — the midterm story
reproduced outside a single process. On security, every one of the unauthorized-access and
abuse probes is refused, the business data is byte-identical before and after each attack,
and the audit chain verifies — none of which the baseline or `ft` build can claim, because
they have no authentication at all.

## 2. Updated requirements, assumptions, and threat model

### 2.1 Requirements

The midterm requirements R1–R6 (availability, bounded failed requests, no lost writes, money
consistency, latency under load, no corrupted transcript shown) still hold and are re-checked
on containers. The endterm adds security and observability requirements, each with a concrete
acceptance test:

| ID | Requirement | Checked by |
|---|---|---|
| R1 | A single crash fails ≤1% of user requests. | X1 |
| R2 | A failure is detected within 1 s; the user-visible interruption is short. | X1, X2 |
| R4 | Money is never inconsistent (no double charge, no debit without a record). | X2, X3, X7 |
| R5 | Under ~5× load, registration and payment p95 latency stays < 1 s. | X4 |
| R7 | Every unauthorized or privilege-exceeding action is denied, and the business data is unchanged afterwards. | X5 |
| R8 | Malformed or abusive input is rejected with a 4xx/429; the service stays up and data stays intact. | X6 |
| R9 | Every injected incident is detected by a defined alert, and the detection delay is recorded. | X2, X4 |
| R10 | The audit trail of security-relevant actions is complete and tamper-evident. | X5, X6 |
| R11 | No service runs with a missing/placeholder secret (fail-closed). | startup check |
| R12 | A network partition causes no split-brain and no data inconsistency. | X8 |

### 2.2 Assumptions

Unchanged from the midterm except where containers change them:

- **Fault model** now includes a **malicious actor** (see the threat model) in addition to
  crash, timing and value faults.
- **Nodes are containers.** "Node A down" / "crash payment-a" kills the process(es) inside
  that container; Docker's restart policy brings it back (the `endterm`/`ft` builds restart
  immediately, the `baseline` after a 20 s delay standing for a manual repair, exactly as in
  the midterm). The database waits ~3 s before restarting so that failover happens first.
- **One host.** All containers run on one machine (Apple M4 Pro, Docker Desktop Linux VM).
  Each container has explicit CPU and memory limits so that the load experiment can actually
  saturate a service, as it did on the midterm's 2-vCPU box.
- **Time is still compressed** (short alert windows, short fault durations) so a run fits in
  ~45 s. Absolute values are specific to this setup; the comparisons across builds are the
  point.
- **TLS termination** at the Internet edge is assumed and out of scope; internal traffic is
  plaintext on isolated Docker networks (discussed as a limitation).

### 2.3 Threat model (summary)

The full threat model is in `docs/endterm/THREAT_MODEL.md`: assets, trust boundaries (a
data-flow diagram), three attacker profiles (anonymous client, authenticated student,
compromised application container), and a STRIDE table of fifteen threats, each mapped to the
control that addresses it and the experiment that verifies it. The assets that matter most
are, as in the midterm, **integrity of money and academic records**; the new insight is that
an *attacker* and a *bug* can both violate integrity, and the same controls (authorization,
least privilege, validation, audit) defend against both.

## 3. Cloud / microservices architecture and resilience design

### 3.1 From processes to containers

In the midterm every instance was an OS process started by a hand-written supervisor. In the
endterm every instance is a **container**, and the orchestration that the supervisor did by
hand is now Docker's:

| Midterm (processes) | Endterm (containers) |
|---|---|
| supervisor starts/stops processes | `docker compose up`, one container per instance |
| supervisor restarts a dead process | `restart: always` (Docker restart policy) |
| `/health/live`, `/health/ready` polled by the gateway/monitor | same endpoints + a Docker `HEALTHCHECK` on every container |
| instances found at `127.0.0.1:<port>` | found by **DNS name** on a Docker network (service discovery) |
| one machine, shared everything | one container per instance, isolated filesystem/network, CPU & memory limits |
| `python -m ftuni up` | `docker compose up` (compose generated from the same topology) |

The topology is still the single source of truth in `ftuni/config.py`;
`deploy/make_compose.py` generates `docker-compose.yml` from it, so the two can never drift.
One Docker **image** holds all the code; the container's command (`payment-a`, `db-b`, …)
selects which instance it is.

### 3.2 Service decomposition, replicas and load balancing

Unchanged in shape from the midterm: a gateway (the only published entry point), four
services (student, payment, records, timetable), two database nodes (primary + synchronous
replica), an external bank, and a monitor. In the `ft` and `endterm` builds each service and
the database run as **two replicas** on logical nodes A and B; the `baseline` runs node A
only. The gateway load-balances round-robin across healthy replicas, detects failures
actively (readiness probes every 0.3 s) and passively (a refused connection marks a replica
down immediately), retries idempotent requests on the other replica, and falls back to a
stale cache or a 503 when a whole service is unavailable. These mechanisms are the midterm's;
the endterm's contribution is to show they still hold across container and network boundaries.

![Endterm architecture: one container per instance, segmented Docker networks, the gateway as the single published entry point and policy-enforcement point.](fig_arch.png)

### 3.3 Health checks, restart and deployment strategy

Each container declares a `HEALTHCHECK` that calls `/health/ready`; readiness means the
instance can do useful work (for a database node: it is the primary, or a replica holding a
complete copy). Docker restarts a crashed container automatically. A deployment/scale change
is a rolling replacement of one replica at a time: because the gateway routes around a
replica that is down and retries on the other, a rolling restart of a service causes no
failed user request (demonstrated as part of X1's masking behaviour).

### 3.4 Network segmentation and least-privilege infrastructure

Four Docker networks enforce who may talk to whom:

- `app` — gateway ↔ services (and the gateway publishes `127.0.0.1:8000`);
- `data` — services ↔ database nodes, `internal: true` (no Internet route, not reachable from the host);
- `bank` — payment ↔ bank, `internal: true`;
- `ops` — Prometheus / Alertmanager / Grafana / alert-sink.

Only the gateway and the monitoring UIs publish ports, and only on loopback. Containers run
as a **non-root** user with a **read-only root filesystem**, `cap_drop: ALL` and
`no-new-privileges`. Each container receives only the Docker **secrets** it needs. This is
the hardware/infrastructure-fault-tolerance idea of the midterm (put copies on independent
failure units, make switching automatic) combined with the security idea of least privilege:
a compromised or crashed container has the smallest possible blast radius.

### 3.5 Graceful degradation and timeouts/retries/breakers

All carried over from the midterm and re-verified on containers: nested timeouts (inner
always shorter than outer), bounded retries with full-jitter backoff for idempotent requests
only, a circuit breaker per dependency, a per-service bulkhead that sheds load instead of
queuing, queued payments (HTTP 202) when the bank is unreachable, and a stale timetable cache.
X3 and X4 exercise these directly.

## 4. Monitoring, logging, metrics, and alerting design

### 4.1 Metrics (≥3, in fact a dozen)

Every service process exposes Prometheus metrics on `/metrics` via `prometheus_client`. The
core three required by the brief, and then some:

- **Request count** — `ftuni_http_requests_total{service,route,method,status}` (so error
  rate is `status=~"5.."` over total).
- **Latency** — `ftuni_http_request_duration_seconds` histogram, labelled by service and
  route, giving p50/p95/p99 via `histogram_quantile`.
- **In-flight requests** — `ftuni_http_inflight{service}`.

Beyond those: circuit-breaker state, database role/epoch/LSN and replication-in-sync, unfinished
payments by status, the monitor's view of every instance, authentication denials by reason,
rate-limited requests, audit records written, and a generic `ftuni_events_total{event}` that
counts every structured log event — so any log event can drive an alert without a bespoke
metric. The gateway additionally exports per-service shed/degraded/retry counters and the
health of each upstream.

### 4.2 Structured logs and correlation IDs

Every process writes one JSON object per event to stdout (collected by `docker logs`) and to a
file. The gateway mints a **request/correlation ID** at the first hop (or sanitises a
client-supplied one — only `[A-Za-z0-9._-]{8,64}`, to prevent log injection), and it is
propagated automatically to the service, the database node and the bank through an httpx
request hook, written into every log line, and returned to the client in `X-Request-ID`. One
ID therefore reconstructs a request's entire path across all containers; `experiments/trace`
and a simple `grep` over the event logs do exactly that (example in the appendix).

### 4.3 Dashboards and alerting

Prometheus scrapes every instance (the gateway's `/metrics` requires the operator token;
service targets come from file-based service discovery written for the running build, so the
baseline is not alerted about replicas it does not have). **Grafana** shows a dashboard
(generated as code, `deploy/grafana/make_dashboard.py`) with rows for the users' view
(request rate, error ratio, p95 per service), instance health, database/payment internals,
and security (denials, rate-limiting, audit records). **Alertmanager** routes every alert to
an **alert-sink** webhook that timestamps its arrival, which is how detection delay is
measured precisely.

The two figures below are the live dashboard during a demonstration run (steady traffic, a
`payment-a` crash at ≈ 21:46:55, a 10 s freeze of the database primary at ≈ 21:47:15, and a
burst of forged-token requests at ≈ 21:47:40). The telling detail is what is *empty*: "Error
ratio (5xx) by service" and "5xx ratio per replica" show **No data** throughout — nothing
reached users as an error — while the lower panels show the machinery that made that true.

![Grafana, users' view and instances. Requests keep flowing; p95 spikes to ~1.6 s only during the DB freeze; the error-ratio panel stays empty (nothing failed for users); the alerts panel shows AuthFailureSpike, HighLatencyP95, InstanceDown and DatabaseFailover firing; scrape-status and the gateway's replica view show db-a and payment-a going red exactly when they were frozen/crashed.](fig_grafana_top.jpg)

![Grafana, database and security rows. The database role/epoch panel shows db-a stepping down and db-b becoming primary with the epoch going 1 → 2 (the failover); "Replication in sync" drops to 0 during the freeze and recovers; "5xx ratio per replica" stays empty; "Refused requests by reason" shows the bad_signature spike from the forged tokens; and "Audit records per second" shows the AUTHN_DENIED spike over the steady PAYMENT/LOGIN trail.](fig_grafana_db_security.jpg)

### 4.4 Alert rules (≥3, in fact 12) — what, threshold, response

The twelve rules (`deploy/prometheus/alerts.yml`) each carry a `summary` and a `response`
annotation. The required "what it detects / threshold / expected response" is encoded there;
representative examples:

| Alert | Detects | Threshold | Expected response |
|---|---|---|---|
| `InstanceDown` | an instance stops answering scrapes | `up == 0` for 2 s | automatic reroute/restart; page if not back in 30 s |
| `UserFacingErrorRate` | users see failures | >5% 5xx per service for 3 s | page on-call; masking has failed |
| `HighLatencyP95` | slow service (R5) | p95 > 1 s for 3 s | check load, slow dependency, CPU |
| `CircuitBreakerOpen` | a dependency is failing fast | breaker open for 2 s | automatic degrade; check the dependency |
| `DatabaseFailover` | the primary changed | any in 1 min | verify the old primary rejoins as replica |
| `DBReplicationLost` | primary writing without a sync replica | `replication_ok == 0` for 5 s | redundancy gone; bring the replica back |
| `AuthFailureSpike` | credential stuffing / token forgery / scanning | >1 denial/s for 2 s | look up actors in the audit log; block source |
| `RateLimitSpike` | a client being throttled | >5 throttles/s for 2 s | identify the account; abusive traffic contained |

A deliberate design point, confirmed experimentally (§8): **automatic masking is faster than
alerting.** A single replica crash is masked by the gateway in milliseconds and recovers in
~1 s — shorter than any sensible alert window (scrape interval + `for:`), so `InstanceDown`
correctly does *not* page for it. Alerts fire for conditions that last: a frozen database
primary (failover), a slow bank (breaker), a load spike (shedding/latency), an attack (auth
failures). This is the right division of labour — machines handle the sub-second events,
humans are told about the ones that persist — and it is visible in the measured detection
delays.

## 5. Security controls and intrusion-tolerance strategy

Seven controls, each closing a concrete weakness that the midterm code had (the midterm
explicitly did not address security). Each is behind a `Policy` flag and present only in the
`endterm` build.

### 5.1 Authentication (JWT) — the gateway as policy-enforcement point

`POST /auth/login` checks a username/password (salted **PBKDF2-SHA256** hashes; the gateway
holds only hashes) and issues a short-lived **JWT** (HS256). Every other request must present
it as a Bearer token. Verification **pins the algorithm** (so `alg:none` and
algorithm-confusion tokens are rejected), requires the issuer and expiry, and checks the role
claim. Weakness closed: in the midterm anyone could call any endpoint.

### 5.2 Authorization — role-based and object-level, default deny

`security.authorize()` encodes who may do what: a student may read and pay only for their own
`sub`; grade changes and the transcript batch are registrar/admin only; anything without an
explicit rule is denied. Crucially, the gateway **strips** any client-supplied
`X-User-*`/`X-Auth-*`/`X-DB-*` headers and sets the identity itself, and does **not** forward
the client's `Authorization` header — so a client cannot assert an identity or smuggle a
credential inward. Services re-check ownership as defence in depth. Weakness closed: IDOR
(reading/altering other students' data) and header-spoofed identity.

### 5.3 Input validation

In the `endterm` build every endpoint uses strict Pydantic models: exact ID/group formats
(regex), amount bounded to `(0, 5 000 000]` and required to be a real integer, `extra="forbid"`
to reject unknown fields, and a 16 KB request-body limit at the gateway. SQL is always
parameterized (it was in the midterm too). Weakness closed: negative/overflow amounts reaching
the bank and ledger, SQL-shaped inputs, oversized bodies.

### 5.4 Rate limiting and abuse protection

A per-user **token bucket** (≈10 req/s, burst 20) returns 429 when exceeded; a separate login
rate limit and a **login lockout** after repeated failures slow brute force. This is *per
identity*, so one abusive student cannot crowd others out — unlike the midterm's per-service
bulkhead, which is about capacity, not fairness. Weakness closed: a single authenticated
client flooding the API.

### 5.5 Least-privilege database access

Each service authenticates to the database with its **own token**, and a SQLite **authorizer**
maps that token to the minimal set of tables and actions it may use (the payment service may
write `payments`/`ledger` and read `students`, nothing else; the monitor is read-only; no
service may run DDL; only the monitor may `/promote`). So even a fully compromised service
container (which can read its own token) cannot change grades, drop tables, dump the database
or force a split brain. Weakness closed: the midterm database ran *any* SQL from *any* caller
with no authentication.

### 5.6 Secrets handling and fail-closed startup

Secrets (JWT key, per-service DB tokens, operator token, Grafana admin password) are Docker
**secrets** mounted as files under `/run/secrets`, never baked into the image or the
repository; `deploy/secrets/` is git-ignored and generated by `deploy/gen_secrets.py`. A
service **refuses to start** (exit 78) if a required secret is missing or still a placeholder,
so the system cannot come up unprotected.

### 5.7 Audit logging — tamper-evident

Every security-relevant decision (login success/failure, authorization denial, payment, grade
change, rate-limiting, operator action, database-ACL denial) is appended to a **hash-chained**
audit log: each record stores the SHA-256 of the previous one, so editing, deleting or
reordering any record breaks the chain from that point and `python -m ftuni.audit verify`
detects it. This extends the midterm's checksum idea (E7) from stored documents to the
security trail. Limitation: someone who can rewrite the whole file can recompute the chain;
production anchors the head hash in external WORM storage.

### 5.8 Operator-surface hardening and supply-chain checks

The fault-injection API (`/chaos`) and the gateway's `/metrics` and `/upstreams` require the
operator token, and in the endterm build `/chaos` is driven only from inside a container
(`ftuni/chaosctl.py`), so the attack surface that made fault injection convenient in the
midterm is not exposed. CI runs `bandit` (static analysis) and `pip-audit` (dependency CVEs).

## 6. Verification and validation plan

### 6.1 Levels of testing

- **Unit tests** (41, `pytest`): the resilience toolkit (breaker states, jittered backoff,
  bulkhead), the storage mirror (corruption detection and repair), the database node
  (duplicate-tx dedup, epoch fencing), and the new security units — password hashing, JWT
  (round-trip, wrong key, `alg:none`, expiry, tampering), the authorization matrix, the rate
  limiter and login lockout, the audit chain (tamper and deletion detection, restart
  continuity), and the database ACL (each service confined to its tables, nobody may DROP,
  only the monitor may promote).
- **Integration tests** (`tests/test_integration.py`, run against a live cluster): login
  required everywhere, the happy path register→pay→transcript, correlation-ID echo,
  idempotent charge-once, idempotency-key binding, IDOR blocked, role separation on grades,
  input validation, and the operator-token gate on `/metrics`.
- **System / fault-injection experiments** (X1–X8): below.

### 6.2 Acceptance criteria and PASS/FAIL

Each experiment has a machine-checked acceptance predicate tied to a requirement (§2.1). For
fault experiments on the `ft`/`endterm` builds the bar is **masked or recovered**: zero failed
user requests *and* a consistent data outcome. For the `baseline` the same experiments are run
to measure the gap, not to pass. Security experiments pass only if **every** probe gets the
expected denial, the business-data checksum is **identical** before and after, and the audit
chain **verifies**. The runner writes a PASS/FAIL per repetition and an aggregate; §8 reports
mean ± spread over repetitions, which addresses the midterm's "each experiment was run once".

### 6.3 Baseline-versus-endterm comparison

Every fault experiment runs on all three builds in the same containerized environment, so the
comparison is fair. `ft` isolates the effect of the midterm's resilience mechanisms;
`endterm` additionally shows that adding the full security stack does **not** regress
resilience. The security experiments run on `endterm` only, because `baseline`/`ft` have no
authentication to test — which is itself the finding.

## 7. Fault-injection and security-test methodology

### 7.1 Environment and procedure

All runs are on one host (Apple M4 Pro, Docker Desktop) with per-container CPU/memory limits.
`experiments/endterm.py` drives each experiment:

1. **Fresh cluster** of the chosen build (`docker compose down -v`, wipe the shared run dir,
   `up`), then wait until *ready* — the gateway sees every replica healthy, Prometheus scrapes
   every instance, the monitor sees every instance ready (the database replica in sync), and
   no alert is firing.
2. **Steady load** at 40 req/s, open-loop (requests fire on schedule), the same seeded mix as
   the midterm (35% profile reads, 15% registrations, 20% payments, 15% transcripts, 15%
   timetables). In the `endterm` build the generator first logs the 300 synthetic students in
   and sends their Bearer tokens.
3. **Inject** the fault at t = 10 s (temporary faults are removed after 12–15 s).
4. Let background recovery finish, run the **consistency checks** and the **data checksum**.
5. **Tear down**, having saved every request, every event, the alerts, the audit log and the
   computed summary.

Each fault experiment was repeated three times per build (the X1 baseline once, since its
~20 s manual-repair outage is deterministic), and the system is restored to a known state
between repetitions by the fresh-cluster step.

**A harness incident, and what we changed.** The shared run directory (logs, audit records,
transcript storage, database files) was first a *host bind mount*. During the first run of
X3 (endterm build, third repetition), at t ≈ 42 s, writes from the records service to that
directory started failing (HTTP 500, then 503 once the gateway's breaker opened), the
post-run replica check could not read the database, and afterwards the host could not even
delete the directory. Docker Desktop's file sharing had failed under the write load of 17
containers; the database and payments kept working to the end of the run, so this was a fault
of the test harness, not of the system under test. We moved the run directory to a **named
Docker volume** (ext4 inside the Docker VM) and copy the evidence out with `docker cp` after
each run. X1 and X2 had completed cleanly and were kept; X3 was re-run in full, and X4, X7 and
X8 ran on the new backend, so the three builds of any one experiment always share the same
environment. The superseded X3 data is archived locally under `results/endterm/_superseded/`; the incident write-up is in `docs/endterm/HARNESS_INCIDENT.md`.

### 7.2 Fault injection without a back door

Fault injection uses the **Docker API**, not an application endpoint, so the system under test
needs no special hooks exposed:

| Experiment | Fault | How it is injected |
|---|---|---|
| X1 | service instance failure | `kill -9 -1` **inside** the `payment-a` container (the app dies, the restart policy brings it back; `docker kill` would count as a manual stop and the policy would not apply) |
| X2 | dependency timeout | `docker pause` the database primary for 12 s (it freezes rather than crashes — a timing fault) |
| X3 | slow external dependency | the bank is told (operator-token chaos, run inside its container) to delay every response 6 s for 15 s |
| X4 | load spike | the workload mix shifts to ~75% timetable at 40 req/s (≈5× the timetable rate) for the run |
| X7 | recovery & consistency | 30% of payments are interrupted right after the bank debit, for 15 s |
| X8 | network partition | `docker network disconnect` the primary from the `data` network for 12 s (both nodes alive, cannot talk) |

Security probes (X5, X6) run against the live gateway, and the insider probes run **inside** a
"compromised" `student-a` container (`experiments/insider.py`), which is the honest way to test
the database ACL and endpoint authorization from a position an attacker could actually reach.

### 7.3 What is measured

- **Detection time** — two numbers: when an internal signal first noticed (the monitor's DOWN
  event, the gateway's first shed, a breaker opening) and when an **alert actually fired**
  (from the alert-sink's arrival timestamp). Reporting both is deliberate (§4.4).
- **Client-visible recovery** — from injection to the end of the last failed request (0 if
  nothing failed).
- **Failed requests** — per user action (a payment and its one retry count once).
- **Latency** — p50/p95/p99 overall, and the p95 of the core services (student+payment) under
  load for R5.
- **Data consistency** — the midterm's money/writes checks, plus a **SHA-256 checksum of every
  business table** taken before and after a security experiment, and an **audit-chain
  verification**.
- **Restart count** — Docker's restart counter for the crashed container, to confirm the
  restart policy fired.

## 8. Results and baseline-versus-endterm comparison

All numbers below are mean ± standard deviation over repetitions (the per-run data and the
generated tables are in `results/endterm/tables.md` and the appendix). Fault experiments ran
on all three builds; security experiments on `endterm`.

### 8.1 Fault experiments (X1–X4, X7, X8)

## Fault-injection experiments (mean ± sd over repetitions)

**X1 — Service instance failure (payment-a killed)**
**X2 — Dependency timeout (database primary frozen 12 s)**
**X3 — Slow external dependency (bank 6 s for 15 s)**
**X4 — Load spike (+180 timetable req/s for 15 s)**
**X7 — Recovery and consistency (30% payments interrupted 15 s)**
**X8 — Network partition (primary cut from the data network 12 s)**

| Exp | Build | Reps | Failed reqs | Detect (alert) s | Detect (monitor) s | Recovery s | p95 ms | Consistent | PASS |
|---|---|---|---|---|---|---|---|---|---|
| X1 | baseline | 1 | 307 | 3.12 | 0.50 | 21.1 | 21 | 1/1 | 0/1 |
| X1 | ft | 3 | 0 | - | 0.38 ± 0.04 | 0.0 | 19 ± 0 | 3/3 | 3/3 |
| X1 | endterm | 3 | 0 | - | 0.43 ± 0.09 | 0.0 | 22 ± 0 | 3/3 | 3/3 |
| X2 | baseline | 3 | 251 ± 2 | 4.05 ± 0.52 | 0.66 ± 0.10 | 12.5 ± 0.1 | 5003 | 0/3 | 0/3 |
| X2 | ft | 3 | 0 | 1.77 ± 0.49 | 0.67 ± 0.11 | 0.0 | 21 ± 0 | 3/3 | 3/3 |
| X2 | endterm | 3 | 0 | 1.95 ± 0.28 | 0.68 ± 0.05 | 0.0 | 41 ± 21 | 3/3 | 3/3 |
| X3 | baseline | 3 | 166 ± 1 | 4.48 ± 0.13 | - | 20.4 ± 0.0 | 5003 ± 0 | 0/3 | 0/3 |
| X3 | ft | 3 | 0 | 4.12 ± 0.29 | 1.80 ± 0.00 | 0.0 | 21 ± 0 | 3/3 | 3/3 |
| X3 | endterm | 3 | 0 | 4.37 ± 0.37 | 2.17 ± 0.01 | 0.0 | 26 ± 0 | 3/3 | 3/3 |
| X4 | baseline | 3 | 2966 ± 33 | 5.43 ± 0.35 | - | 30.0 ± 0.4 | 5005 ± 0 | 0/3 | 0/3 |
| X4 | ft | 3 | 13 ± 9 | 3.92 ± 0.26 | 0.04 ± 0.00 | 0.6 ± 0.4 | 102 ± 12 | 3/3 | 3/3 |
| X4 | endterm | 3 | 10 ± 2 | 4.03 ± 0.84 | 0.04 ± 0.01 | 0.7 ± 0.7 | 113 ± 16 | 3/3 | 3/3 |
| X7 | baseline | 3 | 37 ± 2 | - | 0.72 ± 0.29 | 15.1 ± 0.3 | 23 ± 0 | 0/3 | 0/3 |
| X7 | ft | 3 | 39 ± 6 | 16.96 ± 0.19 | 1.00 ± 0.02 | 15.3 ± 0.5 | 27 ± 1 | 3/3 | 3/3 |
| X7 | endterm | 3 | 34 ± 5 | - | 0.94 ± 0.01 | 15.7 ± 0.2 | 54 ± 3 | 3/3 | 3/3 |
| X8 | baseline | 3 | 460 ± 1 | 4.15 ± 0.63 | 0.51 ± 0.09 | 12.2 ± 0.0 | 21 ± 0 | 0/3 | 0/3 |
| X8 | ft | 3 | 0 | 1.75 ± 0.63 | 0.51 ± 0.03 | 0.0 | 20 ± 0 | 3/3 | 3/3 |
| X8 | endterm | 3 | 0 | 1.96 ± 0.38 | 0.49 ± 0.03 | 0.0 | 22 ± 0 | 3/3 | 3/3 |

Experiment by experiment:

- **X1 — service instance failure.** Killing `payment-a` under load. In the `baseline` the
  single instance is gone until the (compressed) manual restart: 307
  requests failed and the outage lasted 21.1 s. In the `ft` and
  `endterm` builds the gateway rerouted to `payment-b` on a refused connection, the monitor
  flagged the instance down in 0.43 s, and Docker's restart policy
  brought the container back — **0 failed requests**, data consistent. This reproduces the
  midterm's E1 across container boundaries, and confirms the restart policy fires on an
  in-container crash. Note (observability): a single replica crash is masked and recovered in
  ~1 s, below the alert window, so `InstanceDown` does not page for it — correct behaviour.

- **X2 — dependency timeout (frozen database primary).** `docker pause` freezes the primary
  for 12 s (a timing fault, not a crash). The `baseline` cannot fail over: it piled up
  251 failed requests and, worse, ended **inconsistent** every
  repetition — money taken by the bank with no university record (orphan debits) and students
  charged twice, because payments call the bank first and record second. The `endterm` build's
  monitor promoted `db-b`, clients found the new primary, idempotent writes made retries safe:
  **0 failed requests** and consistent data. Here the 12 s freeze does cross the alert windows,
  so `HighLatencyP95`, `InstanceDown` and `UserFacingErrorRate` fire on the baseline at
  ~4 s — detection by alert, as required by R9.

- **X3 — slow external dependency (bank).** The bank delays every response 6 s for 15 s. The
  `baseline`'s long bank timeout makes payments hang until the client gives up and retries,
  double-charging; the `endterm` build's 0.8 s bank timeout and circuit breaker fail fast,
  queue payments (HTTP 202) and let the recovery worker finish each one exactly once when the
  bank recovers. `CircuitBreakerOpen` fires. Result: baseline failed 166, consistent 0/3; endterm failed 0, consistent 3/3.

- **X4 — load spike.** A burst of +180 timetable req/s is added for 15 s on top of the steady
  load (the midterm's E6, which used a second generator). The `baseline` collapses just as it
  did in the midterm: the single timetable instance queues the flood, and because the CPU is
  shared, registrations and payments time out too — **2966 ± 33** failed requests, core p95
  **3601 ms**, and it does not recover until the backlog drains. The `endterm` build sheds the
  timetable flood at the bulkhead (~40 ms after the burst starts), serves the stale timetable
  cache, and keeps the **core services' p95 under the 1 s bar (R5)**: core p95 = **23 ms**.
  Only ~10 timetable requests fail — the group/week combinations not yet cached when the burst
  begins (the same effect as the midterm's 37). `LoadShedding` and `HighLatencyP95` fire.

  *Note:* an earlier version of X4 only shifted the request mix at the normal 40 req/s rate; on
  this (much faster) host that did not saturate anything and the baseline passed. The experiment
  was redesigned with the explicit burst above so that it reproduces the midterm's collapse; the
  weak-load data is archived under `results/endterm/_superseded/`.

- **X7 — recovery and consistency.** 30% of payments are interrupted right after the bank
  debit, for 15 s. Both builds fail a similar number of in-flight attempts; the difference is
  the data. The `baseline` is left with orphan debits and double charges; the `endterm` build
  finishes every interrupted payment exactly once — the student's own retry (same idempotency
  key) completes most, the recovery worker the rest — ending **consistent**. Result:
  baseline failed 37, consistent 0/3; endterm failed 34, consistent 3/3.

- **X8 — network partition (bonus).** `docker network disconnect` cuts the primary from the
  `data` network while both nodes stay alive. This is the case the midterm could not test. The
  epoch fencing token does its job: the partitioned old primary cannot commit (its replica
  acknowledgement fails and it steps down), the monitor promotes the reachable replica, and
  there is **no split brain** and no inconsistency. `DBReplicationLost`/`DatabaseFailover`
  fire. Result: endterm failed 0, consistent 3/3.

![Successful requests per second during each fault (rep 1): baseline (red) drops or collapses, while the ft (blue) and endterm (green) builds stay flat. Grey band = fault active.](fig_timelines.png)

![Endterm build: for each experiment, when the incident was detected internally (monitor/event), when an alert actually fired, and when the last client-visible failure ended. Automatic masking is faster than alerting.](fig_detect_recover.png)

### 8.2 Security experiments (X5, X6)

## Security experiments

| Exp | Build | Cases passed | Data unchanged | Audit chain | Audit records | Alerts | PASS |
|---|---|---|---|---|---|---|---|
| X5 | endterm | 17/17 | yes | ok | 15 | AuthFailureSpike | PASS |
| X6 | endterm | 10/10 | yes | ok | 25 | - | PASS |

- **X5 — unauthorized access.** 17/17 probes, every one denied as expected:
  anonymous requests (401), forged / `alg:none` / wrong-key tokens (401), IDOR on another
  student's profile and transcript (403), a student attempting a grade change or paying for
  someone else (403), header-spoofed identity (403), the operator surface without the token
  (403), and — from inside a *compromised* `student-a` container — direct SQL to the database,
  a `DROP TABLE`, a snapshot dump and a forced replica promotion, all blocked by the
  per-service token and the SQLite authorizer. The business-data checksum was **identical**
  before and after, the audit chain **verified**, and `AuthFailureSpike` fired.

- **X6 — abusive / malformed requests.** 10/10 malformed cases (negative/zero/
  overflow/wrong-type amounts, SQL-shaped IDs, unknown fields, oversized body, non-JSON) all
  rejected with 4xx; and an authenticated flood from one student was throttled by the per-user
  rate limiter (377 of 400 requests throttled with 429), leaving the service up and the data unchanged. Validation
  and rate limiting hold, the audit chain verified.

### 8.3 Requirements verification

## Requirements verification (repetitions meeting the requirement / total)

| Req | Statement | Experiments | baseline | ft | endterm |
|---|---|---|---|---|---|
| R1 | a single crash fails at most 1% of requests | X1 | 0/1 | 3/3 | 3/3 |
| R2 | failure detected within 1 s, user-visible interruption at most 2 s | X1, X2 | 0/4 | 6/6 | 6/6 |
| R4 | money and records stay consistent | X2, X3, X7 | 0/9 | 9/9 | 9/9 |
| R5 | core p95 below 1 s under the load spike | X4 | 0/3 | 3/3 | 3/3 |
| R9 | an incident that outlasts the alert window raises an alert | X2, X3, X4, X8 | 12/12 | 12/12 | 12/12 |
| R12 | network partition: no split brain, data consistent | X8 | 0/3 | 3/3 | 3/3 |
| R7 | every unauthorized action denied, data unchanged | X5 | n/a (no auth) | n/a (no auth) | PASS |
| R8 | malformed and abusive input rejected, service up, data unchanged | X6 | n/a (no auth) | n/a (no auth) | PASS |
| R10 | audit trail complete and tamper-evident | X5 | n/a (no auth) | n/a (no auth) | PASS |

The `endterm` build meets every requirement; the `baseline` fails the resilience and
integrity requirements under fault (R1, R2, R4, R5) and has no notion of R7–R10 at all. The
`ft` build matches `endterm` on resilience, which is the point of the comparison: **adding the
full security stack did not regress resilience** — `endterm` masks the same faults with the
same zero failed requests, at the cost of only a small, constant latency overhead from
authentication and validation on each request.

## 9. Discussion, limitations, and lessons learned

### 9.1 What we learned

1. **Containers did not change the resilience story, which is the good news.** The mechanisms
   designed against process and node failures worked unchanged against container failures,
   pauses and network partitions. The architecture mapped onto Docker directly; the
   hand-written supervisor was simply deleted. The one genuinely new fault — a real network
   partition (X8) — was handled by the fencing token that was already there for failover.

2. **Automatic recovery and human alerting operate on different time scales, and that is
   correct.** The gateway masks a replica crash in milliseconds; a well-tuned alert fires in
   seconds. Trying to alert on the millisecond event would only page humans about things the
   system already fixed. The experiments make this concrete: X1's masked blip fires no page,
   while X2's 12 s freeze and X4's load spike do. Detection, therefore, has two honest numbers,
   and we report both.

3. **The same defences stop a bug and an attacker.** The midterm's worst outcomes were
   integrity failures — money without a record, a silently wrong transcript. The endterm shows
   that authorization, least-privilege database access, input validation and an audit trail
   defend the same integrity against a *malicious* actor: a compromised service still cannot
   change a grade or dump the database, because the database does not trust it.

4. **Defence in depth paid for itself.** No single control is load-bearing. The gateway
   authorizes, but the database also enforces least privilege, so a bypass at the edge still
   fails at the data layer (X5's insider probes prove it). Identity headers are stripped *and*
   set by the gateway, so a spoof cannot cross the boundary.

### 9.2 Trade-offs accepted

- Authentication/validation add a small constant latency to every request; worth it.
- Synchronous replication still trades a write round-trip for never losing a confirmed payment
  (unchanged from the midterm).
- Short alert windows were chosen so a 45 s experiment can trigger them; production would use
  1–5 min windows, trading detection speed for fewer false pages.

### 9.3 Limitations

- **One host.** "Network" between containers is a local bridge; a true multi-host partition and
  real network latency are not modelled. Our failover would need a quorum / third witness to be
  safe against a symmetric partition in production (noted in the threat model).
- **The control plane is still a single point of failure** (one gateway, one monitor). Adding
  authentication did not change that; an active/passive gateway pair and a consensus-based
  failover coordinator remain the next step, as in the midterm.
- **Audit log is tamper-evident, not tamper-proof** against someone who can rewrite the whole
  file; production anchors the head hash externally.
- **No internal TLS** (plaintext on isolated Docker networks); a service mesh / mTLS would
  close the on-path threat.
- **Compressed time** (short fault durations, high synthetic crash rates): absolute values are
  specific to this setup; the cross-build comparisons are the result.
- Experiments were repeated three times per build, not hundreds; small counts (e.g. a few
  failed timetable requests at the start of a burst) still vary between runs, but the large
  effects (0 vs hundreds of failed requests, consistent vs inconsistent) are stable.
- **Two storage backends across experiments.** X1–X2 ran with the run directory on a host bind
  mount, X3–X8 on a named volume (see §7.1). Comparisons are only made *within* an experiment,
  where all builds share one backend; absolute latencies of X1–X2 and X3–X8 should not be
  compared with each other.
- **Not every alert fires in every scenario.** In X6, ten malformed requests are too few to
  cross the `InvalidInputSpike` threshold (more than one per second over 10 s), so that alert did
  not fire; the requests themselves were all rejected and the data was unchanged. A sustained
  fuzzing run would trigger it.

### 9.4 What we would do next

Move to multi-host Kubernetes (the compose mapping is direct), replace the single gateway with
an active/passive pair, move failover to a three-member coordinator (etcd/Patroni), add mTLS
between services, anchor the audit head hash in WORM storage, and run every experiment many
times to report full distributions.

## 10. Conclusion

The endterm took the midterm's fault-tolerant university system and made it a cloud-ready,
observable, secured and systematically verified one, without changing what it is — the same
code base, now configured three ways. On real containers the resilient builds still mask
single-instance crashes, a frozen database primary, a slow dependency, a load spike and a
network partition with zero failed user requests and consistent data, while the baseline loses
hundreds of requests and corrupts money on every fault. Every service now exposes metrics and a
propagated correlation ID, a dozen alert rules watch the system, and the experiments showed —
with measured detection delays — that automatic masking (milliseconds) and human alerting
(seconds) each do their part. A new class of fault, the malicious one, is contained by seven
security controls: every unauthorized and abusive probe is refused, the business data is
byte-identical before and after each attack, and a tamper-evident audit trail records it all.
The most important result is the same as the midterm's, extended: **integrity is the hard
part, and the mechanisms that preserve it against failure — order the write before the side
effect, bind the idempotency key, check the fence, check the checksum, deny by default — are
the same ones that preserve it against an attacker.** The weak points that remain are the
shared control plane and the single host, and they are the next thing to fix.

## 11. References

Carried over from the midterm [1]–[14] (Avižienis et al. on dependability; Koren & Krishna;
Patterson et al. on RAID; Gray; Nygard, *Release It!*; Beyer et al., *SRE*; Brooker on backoff;
Fowler on CircuitBreaker; Leach/Stripe on idempotency; Kubernetes probes; SQLite WAL;
Kleppmann, *DDIA*; Bronson et al. on metastable failures; our Assignments 1–2), plus for the
endterm:

[15] OWASP, *Application Security Verification Standard (ASVS) 4.0* and the *API Security Top 10*.
[16] Microsoft, *The STRIDE Threat Model*.
[17] IETF, RFC 7519 *JSON Web Token (JWT)* and RFC 8725 *JWT Best Current Practices*.
[18] B. Hindman et al. / Docker Inc., *Docker and Compose documentation* (healthchecks, restart policies, networks, secrets).
[19] Prometheus Authors, *Prometheus & Alertmanager documentation* (recording/alerting rules, `histogram_quantile`).
[20] NIST SP 800-162, *Attribute-Based / Role-Based Access Control*; principle of least privilege.

## 12. Appendix

- **A. How to run it** — `docs/endterm/README_ENDTERM.md` (build, run, trace a request, verify
  the audit chain, run tests and experiments, demo script).
- **B. Threat model** — `docs/endterm/THREAT_MODEL.md` (assets, boundaries, STRIDE, controls).
- **C. Architecture** — `results/endterm/fig_arch.png`.
- **D. Alert rules** — `deploy/prometheus/alerts.yml` (what each detects, threshold, response).
- **E. Generated tables and figures** — `results/endterm/tables.md`, `fig_timelines.png`,
  `fig_detect_recover.png`; raw per-run data under `results/endterm/<exp>/<build>/rep*/`
  (`requests.csv`, `events.jsonl`, `summary.json`) and the data checksums / audit records in
  each security experiment's `summary.json`.
- **F. Example request trace** — a single `X-Request-ID` followed across gateway → payment →
  database → bank (one `grep` over the per-container event logs).
