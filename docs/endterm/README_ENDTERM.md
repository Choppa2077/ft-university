# Endterm — how to run it

Extends the midterm FT university system with containers, observability, security and a
verification/fault-injection suite. Same code base, three builds selected by a `Policy`:
`baseline` and `ft` are the midterm behaviour, `endterm` adds the security stack.

## Prerequisites

- Docker Desktop (Compose v2). Tested on an Apple M4 Pro; any machine with Docker works.
- Python 3.11+ with a virtualenv for the host-side runner:
  ```
  python -m venv .venv && . .venv/bin/activate
  pip install httpx prometheus_client PyJWT matplotlib pytest
  ```

## 1. Generate secrets (once; never committed)

```
python deploy/gen_secrets.py        # tokens, JWT key, synthetic users (PBKDF2 hashes)
```
This writes `deploy/secrets/` (git-ignored): the JWT key, per-service DB tokens, the operator
token, the Grafana password, `users.json` (300 synthetic students + a registrar + an admin,
hashes only) and `test_credentials.json` (their plaintext passwords, used only by the load
generator and tests). All data is synthetic.

## 2. Regenerate the compose file and dashboard (if config changed)

```
python deploy/make_compose.py > docker-compose.yml     # from the topology in ftuni/config.py
python deploy/grafana/make_dashboard.py                # dashboard as code
```

## 3. Start a build

```
# endterm build (resilience + security), node A and node B replicas:
FTUNI_MODE=endterm COMPOSE_PROFILES=replicas docker compose up -d --build

# midterm FT build:
FTUNI_MODE=ft       COMPOSE_PROFILES=replicas docker compose up -d

# midterm baseline (node A only):
FTUNI_MODE=baseline docker compose up -d
```

Open:
- **Grafana** http://127.0.0.1:3000 (anonymous viewer; the "FT University — endterm" dashboard is the home)
- **Prometheus** http://127.0.0.1:9090 (targets, rules, alerts)
- **Alertmanager** http://127.0.0.1:9093
- Gateway API http://127.0.0.1:8000 (everything else is on internal networks)

## 4. Use the API (endterm build needs a token)

```
TOK=$(curl -s localhost:8000/auth/login -d '{"username":"S0001","password":"<from test_credentials.json>"}' \
      -H 'content-type: application/json' | jq -r .access_token)
curl -s localhost:8000/students/S0001 -H "Authorization: Bearer $TOK"
curl -s localhost:8000/payments -H "Authorization: Bearer $TOK" \
     -H "Idempotency-Key: demo-1" -d '{"student_id":"S0001","amount":120000}' -H 'content-type: application/json'
```

## 5. Trace one request across all containers

Every response carries `X-Request-ID`; that ID is in every log line on its path. The run
directory (logs, audit, registry, data) lives in the named Docker volume `ftuni_run`, so
search it from inside the cluster:
```
docker exec ftuni-monitor sh -c 'grep -h "<request-id>" /app/run/logs/events/*.jsonl'
```
or copy it out: `docker cp ftuni-monitor:/app/run/. ./run-export`.

## 6. Verify the audit chain

```
docker exec ftuni-monitor python -m ftuni.audit verify /app/run/audit
```

## 7. Run the tests

```
.venv/bin/python -m pytest -q tests                         # 41 unit tests (no cluster needed)
.venv/bin/python -m pytest -q tests/test_integration.py     # against a running endterm cluster
```

## 8. Run the experiments (fresh cluster per run; ~1 min each + restarts)

```
.venv/bin/python -m experiments.endterm                     # X1-X8, all builds, 5 reps
.venv/bin/python -m experiments.endterm X1 X5 --reps 3      # a subset
.venv/bin/python -m experiments.analyze_endterm             # build results/endterm/tables.md + figures
```

Fault injection is done through the Docker API (no back door in the app):
`kill -9 -1` inside a container (crash), `docker pause` (freeze), `docker network disconnect`
(partition). Security probes hit the live gateway; insider probes run inside a "compromised"
`student-a` container.

## 9. Stop / clean up

```
COMPOSE_PROFILES=replicas docker compose down -v
```

## Live demo (what to show)

1. **Container failure** — Grafana open, then `docker exec ftuni-payment-a sh -c 'kill -9 -1'`:
   requests keep succeeding (gateway reroutes), the container restarts, the dashboard shows the
   blip. Contrast with the baseline, where the same kill drops requests for ~20 s.
2. **Database failover** — `docker pause ftuni-db-a`: the monitor promotes db-b, payments keep
   working and stay consistent; `DatabaseFailover` fires.
3. **Attack** — try an unauthorized action (another student's transcript, a forged token, a
   direct DB write from inside student-a); all are refused, the audit log records them,
   `AuthFailureSpike` fires, and the data checksum is unchanged.
