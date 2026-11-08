# Threat model — FT University (endterm)

Scope: the containerized university platform (gateway, student / payment / records /
timetable services, two database nodes, external bank, monitor; Prometheus / Alertmanager
/ Grafana / alert-sink for observability). All data is synthetic. We model misuse and
attack on top of the crash/timing/value faults from the midterm; we do **not** model a
fully compromised host or Docker daemon, a malicious operator with the secret store, or
physical access.

## 1. Assets

| Asset | Why it matters | Primary property |
|---|---|---|
| Academic records (grades, GPA, transcripts) | integrity of a student's record | **Integrity**, Confidentiality |
| Money (payments, ledger, bank debits) | a wrong charge is a real loss | **Integrity** |
| Student PII (profiles, enrollments) | privacy | **Confidentiality** |
| Availability of registration & payments | service during registration week | **Availability** |
| Audit trail | after-the-fact accountability | **Integrity** (tamper-evident) |
| Secrets (JWT key, DB tokens, ops token) | compromise bypasses every control | **Confidentiality** |

## 2. Trust boundaries (data-flow view)

```
            (untrusted Internet)
                   │  HTTPS-terminating edge (out of scope) + JWT
        ┌──────────▼───────────┐   network: app
        │  gateway  (the PEP)  │   authn + authz + rate limit + body limit
        └─────┬────────┬───────┘   strips X-User-*/Authorization, sets identity
   network:app│        │ network: bank
     ┌────────▼──┐  ┌──▼─────┐
     │ services  │  │  bank  │  (external; idempotent debits)
     └────┬──────┘  └────────┘
 network: data (internal, no Internet route)
     ┌────▼───────────────┐
     │ db-a / db-b        │  per-service token + SQLite authorizer (least privilege)
     └────────────────────┘
   network: ops (internal): prometheus, alertmanager, grafana, alert-sink
```

Boundaries: Internet→gateway (strongest; everything is authenticated and validated here);
gateway→services (services trust `X-User-Id/Role` **only because** the gateway removes any
client-supplied copy); services→database (the database does not trust the service — it
checks a per-service token and enforces table-level permission); services→bank (the bank
is idempotent by key). The `data` and `bank` networks are `internal: true`: containers on
them have no route to the Internet and are not reachable from the host except via the gateway.

## 3. Actors

- **A0 Anonymous Internet client** — no valid token.
- **A1 Authenticated student** — a real token for their own account; the main insider-abuse actor (IDOR, privilege escalation, replay, flooding).
- **A2 Compromised application container** (e.g. student-a) — can reach the `data` network and read that container's own DB token. Models an RCE in one service.
- **A3 On-path within a network** — can see traffic on a network it already sits on (no TLS between internal services in this teaching setup; noted as a limitation).

## 4. STRIDE, with the control and the experiment that checks it

| # | Threat (STRIDE) | Actor | Control implemented | Verified by |
|---|---|---|---|---|
| T1 | **S**poofing: forge a token / `alg:none` / wrong key | A0 | JWT HS256, algorithm pinned, issuer + expiry required | X5, `test_security` |
| T2 | **S**poofing: set `X-User-Id`/`X-User-Role` headers | A0,A1 | gateway strips `x-user-*`,`x-auth-*`,`x-db-*` from client requests and sets identity itself | X5 `header_spoof_identity` |
| T3 | **T**ampering: student changes a grade | A1 | RBAC — grade write is registrar/admin only; default deny | X5, `test_authorize` |
| T4 | **T**ampering: direct `UPDATE grades` / `DROP TABLE` on the DB | A2 | per-service DB token + SQLite authorizer (student may not write grades or run DDL) | X5 insider, `test_dbnode_acl` |
| T5 | **R**epudiation: deny having done an action | all | hash-chained audit log (login, payment, grade change, denials, ops) | X5/X6 audit verify |
| T6 | **I**nformation disclosure: read another student's profile/transcript (IDOR) | A1 | object-level authorization (own `sub` only), 404 not 403 on payments | X5 `idor_*`, integration |
| T7 | **I**nformation disclosure: dump the database / snapshot | A2 | snapshot & cross-table reads need a credential the service does not hold (ACL) | X5 insider `snapshot_dump` |
| T8 | **I**nformation disclosure: scrape gateway metrics | A0 | `/metrics`,`/upstreams`,`/chaos` require the operator token | X5 `read_metrics_no_ops_token` |
| T9 | **D**enial of service: one client floods the API | A1 | per-user token bucket (429) + per-service bulkhead; identities are isolated | X6 abuse flood |
| T10 | **D**enial of service: trigger the fault-injection API | A0,A1 | `/chaos` is operator-token only and only reachable inside the container in the endterm build | X5 `ops_endpoint_as_user` |
| T11 | **D**enial of service / corruption: malformed input, SQL in a path, huge body | A0,A1 | strict Pydantic validation (formats, bounds, `extra="forbid"`), 16 KB body limit, parameterized SQL | X6 malformed cases |
| T12 | **E**levation: replay an idempotency key for a different charge | A1 | key is bound to payer + amount; mismatch → 422 | X6 / integration |
| T13 | **E**levation: brute-force a password | A0 | PBKDF2 hashes + login lockout after repeated failures | `test_security` |
| T14 | **E**levation: a stolen service token calls `/promote` (split brain) | A2 | endpoint ACL — only the monitor may promote/demote | X5 insider `promote_replica` |
| T15 | **T**ampering (supply/secret): run with no/weak secret | operator error | services fail closed (exit 78) on a missing or placeholder secret | `ftuni/run.py` |

## 5. Least privilege, concretely

- Containers run as a non-root user, read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`.
- Network segmentation: the database and bank are on `internal` networks; only the gateway (and the monitoring UIs) publish ports, bound to `127.0.0.1`.
- Each service holds exactly one DB token; the authorizer maps that token to the minimal set of tables and actions it needs (e.g. the payment service may write `payments`/`ledger` and read `students`, nothing else; the monitor is read-only).
- The CI pipeline runs `bandit` (code) and `pip-audit` (dependencies).

## 6. Residual risk / out of scope

- No TLS between internal services (A3 on-path): acceptable in a single-host teaching
  setup; production would use a service mesh / mTLS.
- A full host or Docker-daemon compromise defeats network isolation and can read all
  secrets; the audit log is tamper-evident but not tamper-proof against someone who can
  rewrite the whole file (production anchors the head hash in WORM storage).
- The control plane (single gateway, single monitor) remains a single point of failure, as
  in the midterm. Authentication does not remove that; it is discussed in the report.
- No per-field encryption at rest; the threat of a stolen disk is not modeled.
