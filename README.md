# Resilient University Information System

Course project for *Fault Tolerance and Dependable Computing* (AITU, 2026–2027), developed in
three stages on one code base:

| Stage | What it added | Report | Git |
|---|---|---|---|
| Midterm | baseline vs fault-tolerant build (replicas, DB replication + failover, timeouts, retries, breakers, idempotency, checkpointing, rollback, degradation); 7 failure experiments; reliability analysis | `docs/Midterm_Report_FT_University.docx` | tag `midterm` |
| Endterm | containers (Docker Compose), observability (Prometheus, Grafana, 12 alert rules, request IDs), 7 security controls, experiments X1–X8 | `docs/endterm/Endterm_Report.docx` | tag `endterm-submission` |
| **Final** | SLOs, FMEA and single-point-of-failure analysis, reliability measured on containers (soak + RBD), steady-state overhead (X0) and gray-failure (X9) experiments, mechanism-activation counts, alert correctness, all experiments × 3 builds × 5 repetitions | **`docs/final/Final_Report.docx`** | branch `final` |

A small university platform — student registration, tuition payments, transcripts,
timetables — runs as one container per instance behind a gateway. One `Policy` object
(`ftuni/config.py`) selects one of three builds from the same code: `baseline` (one copy of
everything, no protection), `ft` (the fault-tolerant build) and `endterm` (`ft` + security
controls, the improved configuration). All data is synthetic.

## Repository map

```
ftuni/                the system (one package; every container runs `python -m ftuni.run <instance>`)
  services/           student, payment, records, timetable, bank
  gateway.py          entry point: authn/authz, validation, rate limits, load balancing, retries, breakers, bulkheads, stale cache
  dbnode.py, monitor.py, dbclient.py   replicated SQLite node, failover controller, client with failover
  resilience.py, storage.py, security.py, audit.py, obs.py, config.py
deploy/               compose generator, Prometheus rules, Alertmanager, Grafana dashboard-as-code, secret generator
experiments/          workload, fault injection, consistency checks, campaigns, analysis
  final.py            final campaign X0-X9      soak_docker.py   crash soak on containers
  analyze_final.py    tables, alert correctness, reliability, SLO verdicts, figures
  endterm.py          endterm campaign (reused by final.py)        security_tests.py, insider.py
tests/                unit tests (58) and integration tests against a live cluster (10)
docs/final/           final report, SLOs, fault model (FMEA), test plan, figure script
docs/endterm/         endterm report, threat model (STRIDE), run instructions
results/final/        final campaign: per-run summaries, aggregates, tables.md, report_data.json, figures, raw/
results/endterm/      endterm campaign            results/E*_*, soak_*   midterm results
```

## Requirements

- Docker Desktop (Compose v2). Tested on Docker 28.5 (Apple M4 Pro, VM with 12 vCPU, ~7.7 GiB).
- Python 3.11+ on the host for the experiment tools:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Quick start (improved build)

Secrets are generated locally once and are never committed (`deploy/secrets/` is git-ignored):

```bash
python deploy/gen_secrets.py
docker build -t ftuni:endterm .
FTUNI_MODE=endterm COMPOSE_PROFILES=replicas docker compose up -d
docker compose ps --format 'table {{.Name}}\t{{.Status}}'
```

Then open Grafana at http://127.0.0.1:3000 (the "FT University - endterm" dashboard is the home
page) and Prometheus at http://127.0.0.1:9090/alerts. The API is at http://127.0.0.1:8000:

```bash
export S1PW=$(python -c "import json;print(json.load(open('deploy/secrets/test_credentials.json'))['S0001'])")
TOK=$(curl -s localhost:8000/auth/login -H 'content-type: application/json' -d "{\"username\":\"S0001\",\"password\":\"$S1PW\"}" | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s localhost:8000/students/S0001 -H "Authorization: Bearer $TOK"
python -m experiments.demo_traffic
```

`demo_traffic` prints one line per second with successful and failed requests; Ctrl+C stops it and
prints a summary.

Other builds (for comparison):

```bash
FTUNI_MODE=ft COMPOSE_PROFILES=replicas docker compose up -d
FTUNI_MODE=baseline docker compose up -d
```

Always stop with the `replicas` profile, otherwise node-B containers and the run volume survive:

```bash
COMPOSE_PROFILES=replicas docker compose down -v --remove-orphans
```

## Tests

```bash
python -m pytest -q tests
```

Without a running cluster this runs the 58 unit tests (the integration tests are skipped); with the
`endterm` cluster up it also runs the 10 integration tests.

## Reproducing the final results

```bash
python -m experiments.final
python -m experiments.soak_docker
python -m experiments.analyze_final
```

- `experiments.final` runs X0–X4 and X7–X9 on all three builds × 5 repetitions and X5–X6 (security)
  × 3, each repetition on a fresh cluster (about 3.5 hours). It can be interrupted and restarted:
  repetitions that already have a `summary.json` are skipped. Options: experiment ids (`X2 X9`),
  `--builds endterm`, `--reps 1`, `--out <dir>`.
- `experiments.soak_docker` runs the 900 s crash soak for `baseline` and `endterm` (about 35 min).
- `experiments.analyze_final` writes `results/final/tables.md`, `report_data.json`,
  `fig_timelines.png` and `fig_soak.png`.
- Keep the machine awake and do not run other Docker workloads meanwhile (the campaign uses fixed
  container names and ports). The environment of the published run is in
  `results/final/environment.json`.

| Experiment | Fault |
|---|---|
| X0 | none — steady state (overhead, false-positive alerts) |
| X1 | `kill -9` inside `payment-a` (service instance failure) |
| X2 | database primary frozen 12 s (`docker pause`) |
| X3 | bank answers 6 s late for 15 s |
| X4 | +180 timetable req/s for 15 s (load spike) |
| X5 | 17 unauthorized / privilege-escalation probes, incl. a compromised container |
| X6 | 10 malformed / abusive probes and a request flood |
| X7 | 30 % of payments interrupted right after the bank debit, 15 s |
| X8 | database primary disconnected from the data network 12 s (partition) |
| X9 | gray failure: one replica answers 50 % of requests with HTTP 500 while health checks stay green |

<!--README_RESULTS-->

## Documents

- Final report: `docs/final/Final_Report.docx` (source `Final_Report.md`); objectives
  `docs/final/REQUIREMENTS_AND_SLOS.md`; fault model and FMEA `docs/final/FAULT_MODEL.md`; test plan
  `docs/final/TEST_PLAN.md`; threat model `docs/endterm/THREAT_MODEL.md`.
- Build the report: `bash docs/final/to_docx.sh` (needs pandoc).

## Midterm process mode (no Docker)

The midterm ran every instance as a plain process under a small supervisor; this still works:

```bash
pip install -r requirements.txt
python -m ftuni up --mode ft
python -m experiments.demo db
python -m experiments.run_experiments
python -m experiments.soak --mode both
python -m experiments.analyze
```

Midterm results (one run per build, process mode):

| Exp | Baseline: failed user requests / data | FT: failed user requests / data |
|---|---|---|
| E1 app crash | 429 / 1600, 20.5 s outage | 0 / 1600 |
| E2 DB primary down | 666 / 1600; 259 debits without a payment, 133 double charges | 0 / 1600, failover in 0.53 s |
| E3 slow bank | 76 / 1600; 119 double charges | 0 / 1600; 134 payments queued and completed later |
| E4 node A down | 844 / 1800 | 0 / 1800 |
| E5 interrupted payments | 53 debits without a payment, 45 double charges | consistent; every payment finished within 1.9 s |
| E6 overload | 3154 / 3850, did not recover | 37 / 3850; 1664 stale-cache answers |
| E5b batch killed | restarted from zero, 151 transcripts redone | resumed from checkpoint 150, 2 redone |
| E7 corrupted files | 25 wrong GPAs served silently | 30/30 detected and repaired |
